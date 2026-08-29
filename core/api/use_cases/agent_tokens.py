"""Per-principal agent-token lifecycle, independent of FastAPI.

The raw bearer value exists only in the return value of ``create_token``. The
database stores its SHA-256 digest plus explicit principal, workspace, scope,
expiry, rotation, acknowledgement, and revocation state.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from core.api.config import settings
from core.api.models import AgentTokenCreateRequest, AgentTokenResponse
from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ServiceUnavailableError,
    ValidationError,
)

_TOKEN_BYTES = 32
_DEFAULT_ROTATION_OVERLAP_MINUTES = 60
GRAPH_INGEST_SCOPE = "graph:ingest"
GRAPH_INGEST_TOKEN_ID_PREFIX = "grf_"
GRAPH_INGEST_LOCAL_TOKEN_ID_PREFIX = "grf_local_"
_GRAPH_INGEST_LABEL = "graph-ingest"
_GRAPH_INGEST_LIFETIME_HOURS = 1
_SELECT_FIELDS = (
    "id, agent_name, token_hash, scopes, is_active, created_at, last_used_at, "
    "workspace_id, principal_id, principal_type, label, issued_at, expires_at, "
    "revoked_at, revoked_by, rotation_family_id, supersedes_id, overlap_until, "
    "acknowledged_at, acknowledgement_actor, credential_kind"
)


def hash_token(token: str) -> str:
    """Return the irreversible SHA-256 digest stored for a bearer value."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_lifecycle_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_to_response(
    row: aiosqlite.Row, *, raw_token: str | None = None
) -> AgentTokenResponse:
    try:
        parsed_scopes = json.loads(row["scopes"] or "[]")
    except (json.JSONDecodeError, TypeError):
        parsed_scopes = []
    scopes = [value for value in parsed_scopes if isinstance(value, str)]
    return AgentTokenResponse(
        id=row["id"],
        agent_name=row["agent_name"],
        scopes=scopes,
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        token=raw_token,
        principal_id=row["principal_id"],
        principal_type=row["principal_type"],
        label=row["label"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        rotation_family_id=row["rotation_family_id"],
        supersedes_id=row["supersedes_id"],
        overlap_until=row["overlap_until"],
        acknowledged_at=row["acknowledged_at"],
        credential_kind=row["credential_kind"],
    )


async def _fetch_token(
    db: aiosqlite.Connection,
    token_id: str,
    workspace_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        f"SELECT {_SELECT_FIELDS} FROM agent_tokens WHERE id = ? AND workspace_id = ?",
        (token_id, workspace_id),
    )
    return await cursor.fetchone()


async def _resolve_principal(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    slug: str,
) -> aiosqlite.Row:
    cursor = await db.execute(
        "SELECT id, slug, type FROM users WHERE slug = ? AND deleted_at IS NULL "
        "AND workspace_id = ?",
        (slug, workspace_id),
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        raise NotFoundError(
            code="token_principal_not_found",
            message="The token principal is not available in this workspace.",
        )
    return rows[0]


async def _resolve_principal_id(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    principal_id: str,
) -> aiosqlite.Row:
    cursor = await db.execute(
        "SELECT id, slug, type, system_role, auth_provider FROM users "
        "WHERE id = ? AND deleted_at IS NULL AND workspace_id = ?",
        (principal_id, workspace_id),
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        raise NotFoundError(
            code="token_principal_not_found",
            message="The token principal is not available in this workspace.",
        )
    return rows[0]


async def _resolve_graph_ingest_principal(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
) -> aiosqlite.Row:
    """Resolve a person or materialize one supported MCP service identity."""
    is_service_caller = (
        ctx.user_type == "agent" and not ctx.is_human_session and bool(ctx.username)
    )
    if not is_service_caller:
        return await _resolve_principal_id(
            db,
            workspace_id=workspace_id,
            principal_id=ctx.user_id,
        )

    try:
        if not db.in_transaction:
            await db.execute("BEGIN IMMEDIATE")
        try:
            principal = await _resolve_principal_id(
                db,
                workspace_id=workspace_id,
                principal_id=ctx.user_id,
            )
        except NotFoundError:
            principal = None

        slug = (
            "local"
            if ctx.is_local_os_account
            else "mcp-"
            + hashlib.sha256(f"{workspace_id}\0{ctx.user_id}".encode()).hexdigest()[:24]
        )
        if principal is None:
            await db.execute(
                "INSERT INTO users "
                "(id, slug, display_name, type, system_role, auth_provider, "
                "workspace_id) VALUES (?, ?, ?, 'agent', ?, 'mcp_service', ?)",
                (
                    ctx.user_id,
                    slug,
                    ctx.username,
                    ctx.system_role,
                    workspace_id,
                ),
            )
        elif ctx.is_local_os_account:
            if principal["slug"] != "local" or principal["type"] not in {
                "human",
                "agent",
            }:
                raise ConflictError(
                    code="token_principal_conflict",
                    message="The local MCP principal conflicts with an existing identity.",
                )
        elif principal["auth_provider"] == "mcp_service":
            if principal["slug"] != slug or principal["type"] != "agent":
                raise ConflictError(
                    code="token_principal_conflict",
                    message="The MCP service principal conflicts with an existing identity.",
                )
            await db.execute(
                "UPDATE users SET system_role = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND workspace_id = ? AND auth_provider = 'mcp_service'",
                (ctx.system_role, ctx.user_id, workspace_id),
            )
        elif (
            principal["type"] != ctx.user_type
            or principal["system_role"] != ctx.system_role
        ):
            raise ConflictError(
                code="token_principal_conflict",
                message="The MCP caller conflicts with an existing principal.",
            )

        principal = await _resolve_principal_id(
            db,
            workspace_id=workspace_id,
            principal_id=ctx.user_id,
        )
        if ctx.is_local_os_account:
            readback_valid = principal["slug"] == "local" and principal["type"] in {
                "human",
                "agent",
            }
        elif principal["auth_provider"] == "mcp_service":
            readback_valid = (
                principal["slug"] == slug
                and principal["type"] == "agent"
                and principal["system_role"] == ctx.system_role
            )
        else:
            readback_valid = (
                principal["type"] == ctx.user_type
                and principal["system_role"] == ctx.system_role
            )
        if not readback_valid:
            raise ServiceUnavailableError(
                code="token_principal_readback_failed",
                message="The MCP service principal could not be confirmed.",
            )
        return principal
    except ServiceError:
        await db.rollback()
        raise
    except aiosqlite.IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            code="token_principal_conflict",
            message="The MCP service principal changed concurrently; retry.",
        ) from exc
    except aiosqlite.Error as exc:
        await db.rollback()
        raise ServiceUnavailableError(
            code="token_store_unavailable",
            message="The token store is temporarily unavailable.",
        ) from exc


def _bounded_lifetime(body: AgentTokenCreateRequest) -> int:
    lifetime = (
        body.expires_in_hours
        if body.expires_in_hours is not None
        else settings.agent_token_default_lifetime_hours
    )
    if lifetime > settings.agent_token_max_lifetime_hours:
        raise ValidationError(
            code="token_lifetime_too_long",
            message="The requested token lifetime exceeds the configured maximum.",
        )
    return lifetime


def _bounded_overlap(body: AgentTokenCreateRequest) -> int:
    requested = (
        body.overlap_minutes
        if body.overlap_minutes is not None
        else _DEFAULT_ROTATION_OVERLAP_MINUTES
    )
    if requested > settings.agent_token_max_overlap_minutes:
        raise ValidationError(
            code="token_overlap_too_long",
            message="The requested rotation overlap exceeds the configured maximum.",
        )
    return requested


async def _prepare_rotation(
    db: aiosqlite.Connection,
    *,
    body: AgentTokenCreateRequest,
    principal_id: str,
    workspace_id: str,
    actor: str,
    now: datetime,
) -> tuple[str, str | None, str | None, str | None]:
    if body.supersedes_id is None:
        return "", None, None, None

    predecessor = await _fetch_token(db, body.supersedes_id, workspace_id)
    if predecessor is None or predecessor["principal_id"] != principal_id:
        raise NotFoundError(
            code="rotation_predecessor_not_found",
            message="The rotation predecessor is not owned by this principal.",
        )
    if not bool(predecessor["is_active"]) or predecessor["revoked_at"]:
        raise ConflictError(
            code="rotation_predecessor_inactive",
            message="The rotation predecessor is already inactive.",
        )
    if predecessor["supersedes_id"] and predecessor["acknowledged_at"] is None:
        raise ConflictError(
            code="rotation_predecessor_unacknowledged",
            message=(
                "An unacknowledged successor cannot start another rotation. "
                "Acknowledge it or replace it from its direct predecessor."
            ),
        )
    if predecessor["expires_at"]:
        predecessor_expiry = _parse_lifecycle_time(predecessor["expires_at"])
        if predecessor_expiry is None:
            raise ConflictError(
                code="rotation_predecessor_invalid",
                message="The rotation predecessor has invalid lifecycle data.",
            )
        if predecessor_expiry <= now:
            raise ConflictError(
                code="rotation_predecessor_expired",
                message="The rotation predecessor has expired.",
            )
    successor = await (
        await db.execute(
            "SELECT id, acknowledged_at FROM agent_tokens WHERE supersedes_id = ? "
            "AND workspace_id = ? AND is_active = 1 AND revoked_at IS NULL",
            (body.supersedes_id, workspace_id),
        )
    ).fetchone()
    replaced_successor_id = None
    if successor is not None:
        if successor["acknowledged_at"] is not None:
            raise ConflictError(
                code="rotation_already_started",
                message="This token already has an acknowledged rotation successor.",
            )
        replaced_successor_id = successor["id"]
        await db.execute(
            "UPDATE agent_tokens SET is_active = 0, revoked_at = ?, revoked_by = ? "
            "WHERE id = ? AND workspace_id = ? AND acknowledged_at IS NULL "
            "AND is_active = 1 AND revoked_at IS NULL",
            (now.isoformat(), actor, replaced_successor_id, workspace_id),
        )

    overlap_minutes = _bounded_overlap(body)
    delivery_ack_deadline = (
        now + timedelta(seconds=max(60, overlap_minutes * 60))
    ).isoformat()
    family_id = predecessor["rotation_family_id"] or predecessor["id"]
    # The predecessor remains usable until the successor proves delivery by
    # authenticating its acknowledgement. The bounded deadline belongs to the
    # unacknowledged successor, never to the still-known predecessor credential.
    await db.execute(
        "UPDATE agent_tokens SET overlap_until = NULL WHERE id = ? "
        "AND workspace_id = ? AND is_active = 1 AND revoked_at IS NULL",
        (body.supersedes_id, workspace_id),
    )
    return (
        family_id,
        body.supersedes_id,
        delivery_ack_deadline,
        replaced_successor_id,
    )


async def _issue_token(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    body: AgentTokenCreateRequest,
    principal: aiosqlite.Row,
    personal: bool,
    token_id_prefix: str | None = None,
) -> AgentTokenResponse:
    """Persist one token through the shared lifecycle and return plaintext once."""
    from core.api.services.audit import log_audit

    workspace_id = require_workspace_ctx(ctx)

    now = _now()
    expiry = now + timedelta(hours=_bounded_lifetime(body))
    resolved_prefix = token_id_prefix or ("pat_" if personal else "agt_tok_")
    token_id = f"{resolved_prefix}{uuid.uuid4().hex[:16]}"
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    token_hash = hash_token(raw_token)

    try:
        if not db.in_transaction:
            await db.execute("BEGIN IMMEDIATE")
        (
            family_id,
            supersedes_id,
            delivery_ack_deadline,
            replaced_successor_id,
        ) = await _prepare_rotation(
            db,
            body=body,
            principal_id=principal["id"],
            workspace_id=workspace_id,
            actor=ctx.username,
            now=now,
        )
        if not family_id:
            family_id = token_id
        await db.execute(
            "INSERT INTO agent_tokens ("
            "id, agent_name, token_hash, scopes, is_active, created_at, workspace_id, "
            "principal_id, principal_type, label, issued_at, expires_at, "
            "rotation_family_id, supersedes_id, overlap_until, credential_kind"
            ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'individual')",
            (
                token_id,
                principal["slug"],
                token_hash,
                json.dumps(body.scopes, separators=(",", ":")),
                now.isoformat(),
                workspace_id,
                principal["id"],
                principal["type"] or "human",
                body.agent_name,
                now.isoformat(),
                expiry.isoformat(),
                family_id,
                supersedes_id,
                delivery_ack_deadline,
            ),
        )
        await log_audit(
            db,
            action="agent_token.rotate" if supersedes_id else "agent_token.create",
            user=ctx.username,
            resource_type="agent_token",
            resource_id=token_id,
            details={
                "principal_id": principal["id"],
                "principal_type": principal["type"] or "human",
                "expires_at": expiry.isoformat(),
                "scope_count": len(body.scopes),
                "rotation_family_id": family_id,
                "supersedes_id": supersedes_id,
                "delivery_ack_deadline": delivery_ack_deadline,
                "replaced_unacknowledged_successor_id": replaced_successor_id,
            },
            workspace_id=workspace_id,
        )
        row = await _fetch_token(db, token_id, workspace_id)
        if row is None:
            raise ServiceUnavailableError(
                code="token_readback_failed",
                message="The token could not be confirmed after creation.",
            )
        response = _row_to_response(row, raw_token=raw_token)
        await db.commit()
        return response
    except ServiceError:
        await db.rollback()
        raise
    except aiosqlite.IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            code="token_lifecycle_conflict",
            message="The token lifecycle changed concurrently; retry from current state.",
        ) from exc
    except aiosqlite.Error as exc:
        await db.rollback()
        raise ServiceUnavailableError(
            code="token_store_unavailable",
            message="The token store is temporarily unavailable.",
        ) from exc
    except Exception:
        # No plaintext leaves this function unless every persisted field can be
        # reconstructed into the declared response. Keep unexpected response or
        # audit failures from leaving an issuable credential in an open transaction.
        await db.rollback()
        raise


async def create_token(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    body: AgentTokenCreateRequest,
    personal: bool,
) -> AgentTokenResponse:
    """Issue one bounded token and return its plaintext exactly once."""
    if not personal:
        require_role_ctx(ctx, "admin", "super_admin")
        if ctx.user_type != "human" or not ctx.is_human_session:
            raise AuthorizationError(
                code="human_session_required",
                message="Administrative token issuance requires a human session.",
            )

    workspace_id = require_workspace_ctx(ctx)
    if personal:
        if not ctx.is_human_session:
            requested_scopes = set(body.scopes)
            caller_scopes = set(ctx.scopes)
            if not requested_scopes.issubset(caller_scopes):
                raise AuthorizationError(
                    code="token_scope_escalation",
                    message=(
                        "A bearer-authenticated principal may only issue a "
                        "personal token with an equal or narrower scope."
                    ),
                )
        if not ctx.user_id:
            raise AuthorizationError(
                code="token_principal_unbound",
                message="The authenticated principal has no stable identifier.",
            )
        principal = await _resolve_principal(
            db, workspace_id=workspace_id, slug=ctx.username
        )
        if principal["id"] != ctx.user_id:
            raise AuthorizationError(
                code="token_principal_mismatch",
                message="The authenticated principal no longer matches the workspace record.",
            )
    else:
        principal = await _resolve_principal(
            db, workspace_id=workspace_id, slug=body.agent_name
        )
        if (principal["type"] or "human") != "agent":
            raise ValidationError(
                code="agent_principal_required",
                message="Administrative agent tokens can only target an agent principal.",
            )

    return await _issue_token(
        ctx,
        db,
        body=body,
        principal=principal,
        personal=personal,
    )


async def mint_graph_ingest_token(
    ctx: CallerContext,
    db: aiosqlite.Connection,
) -> AgentTokenResponse:
    """Mint a caller-bound, one-hour credential for graph ingest only."""
    require_role_ctx(ctx, "operator")
    workspace_id = require_workspace_ctx(ctx)
    if not ctx.user_id:
        raise AuthorizationError(
            code="token_principal_unbound",
            message="The authenticated principal has no stable identifier.",
        )
    principal = await _resolve_graph_ingest_principal(
        ctx,
        db,
        workspace_id=workspace_id,
    )
    body = AgentTokenCreateRequest(
        agent_name=_GRAPH_INGEST_LABEL,
        scopes=[GRAPH_INGEST_SCOPE],
        expires_in_hours=_GRAPH_INGEST_LIFETIME_HOURS,
    )
    return await _issue_token(
        ctx,
        db,
        body=body,
        principal=principal,
        personal=True,
        token_id_prefix=(
            GRAPH_INGEST_LOCAL_TOKEN_ID_PREFIX
            if ctx.is_local_os_account
            else GRAPH_INGEST_TOKEN_ID_PREFIX
        ),
    )


async def list_tokens(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    personal: bool,
) -> list[AgentTokenResponse]:
    workspace_id = require_workspace_ctx(ctx)
    now = _now().isoformat()
    parameters: list[str] = [workspace_id, now, now]
    owner_clause = ""
    if personal:
        if not ctx.user_id:
            raise AuthorizationError(
                code="token_principal_unbound",
                message="The authenticated principal has no stable identifier.",
            )
        owner_clause = " AND principal_id = ?"
        parameters.append(ctx.user_id)
    else:
        require_role_ctx(ctx, "admin", "super_admin")
        if ctx.user_type != "human" or not ctx.is_human_session:
            raise AuthorizationError(
                code="human_session_required",
                message="Administrative token listing requires a human session.",
            )
    try:
        cursor = await db.execute(
            f"SELECT {_SELECT_FIELDS} FROM agent_tokens WHERE workspace_id = ? "
            "AND is_active = 1 AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "AND (overlap_until IS NULL OR acknowledged_at IS NOT NULL "
            "OR overlap_until > ?)"
            f"{owner_clause} ORDER BY created_at DESC",
            parameters,
        )
        return [_row_to_response(row) for row in await cursor.fetchall()]
    except aiosqlite.Error as exc:
        raise ServiceUnavailableError(
            code="token_store_unavailable",
            message="The token store is temporarily unavailable.",
        ) from exc


async def revoke_token(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    token_id: str,
    personal: bool,
) -> None:
    from core.api.services.audit import log_audit

    workspace_id = require_workspace_ctx(ctx)
    if not personal:
        require_role_ctx(ctx, "admin", "super_admin")
        if ctx.user_type != "human" or not ctx.is_human_session:
            raise AuthorizationError(
                code="human_session_required",
                message="Administrative token revocation requires a human session.",
            )
    try:
        if not db.in_transaction:
            await db.execute("BEGIN IMMEDIATE")
        row = await _fetch_token(db, token_id, workspace_id)
        if row is None or (personal and row["principal_id"] != ctx.user_id):
            raise NotFoundError(
                code="token_not_found",
                message="The token is not available to this principal.",
            )
        if not row["revoked_at"]:
            revoked_at = _now().isoformat()
            await db.execute(
                "UPDATE agent_tokens SET is_active = 0, revoked_at = ?, revoked_by = ? "
                "WHERE id = ? AND workspace_id = ? AND revoked_at IS NULL",
                (revoked_at, ctx.username, token_id, workspace_id),
            )
            await log_audit(
                db,
                action="agent_token.revoke",
                user=ctx.username,
                resource_type="agent_token",
                resource_id=token_id,
                details={"principal_id": row["principal_id"]},
                workspace_id=workspace_id,
            )
        await db.commit()
    except ServiceError:
        await db.rollback()
        raise
    except aiosqlite.Error as exc:
        await db.rollback()
        raise ServiceUnavailableError(
            code="token_store_unavailable",
            message="The token store is temporarily unavailable.",
        ) from exc


async def acknowledge_rotation(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    token_id: str,
    authenticated_token_id: str | None,
) -> AgentTokenResponse:
    """Acknowledge the new credential and atomically revoke its predecessor."""
    from core.api.services.audit import log_audit

    if authenticated_token_id != token_id:
        raise AuthorizationError(
            code="new_token_required",
            message="Rotation acknowledgement must authenticate with the new token.",
        )
    workspace_id = require_workspace_ctx(ctx)
    try:
        if not db.in_transaction:
            await db.execute("BEGIN IMMEDIATE")
        row = await _fetch_token(db, token_id, workspace_id)
        now = _now()
        if (
            row is None
            or row["principal_id"] != ctx.user_id
            or row["credential_kind"] != "individual"
            or not row["supersedes_id"]
            or not bool(row["is_active"])
            or row["revoked_at"] is not None
        ):
            raise NotFoundError(
                code="rotation_not_found",
                message="No acknowledgable rotation exists for this token.",
            )
        expires_at = _parse_lifecycle_time(row["expires_at"])
        if expires_at is None or expires_at <= now:
            raise NotFoundError(
                code="rotation_not_found",
                message="No acknowledgable rotation exists for this token.",
            )
        if row["acknowledged_at"] is None:
            acknowledgement_deadline = _parse_lifecycle_time(row["overlap_until"])
            if acknowledgement_deadline is None or acknowledgement_deadline <= now:
                raise NotFoundError(
                    code="rotation_not_found",
                    message="No acknowledgable rotation exists for this token.",
                )
            acknowledged_at = now.isoformat()
            updated_successor = await db.execute(
                "UPDATE agent_tokens SET acknowledged_at = ?, acknowledgement_actor = ?, "
                "overlap_until = NULL "
                "WHERE id = ? AND workspace_id = ? AND principal_id = ? "
                "AND credential_kind = 'individual' AND supersedes_id = ? "
                "AND acknowledged_at IS NULL AND is_active = 1 AND revoked_at IS NULL "
                "AND expires_at = ? AND overlap_until = ?",
                (
                    acknowledged_at,
                    ctx.username,
                    token_id,
                    workspace_id,
                    ctx.user_id,
                    row["supersedes_id"],
                    row["expires_at"],
                    row["overlap_until"],
                ),
            )
            if updated_successor.rowcount != 1:
                raise ConflictError(
                    code="rotation_acknowledgement_conflict",
                    message="The token lifecycle changed concurrently; retry from current state.",
                )
            revalidated_at = _now()
            if (
                expires_at <= revalidated_at
                or acknowledgement_deadline <= revalidated_at
            ):
                raise NotFoundError(
                    code="rotation_not_found",
                    message="No acknowledgable rotation exists for this token.",
                )
            await db.execute(
                "UPDATE agent_tokens SET is_active = 0, revoked_at = ?, revoked_by = ? "
                "WHERE id = ? AND workspace_id = ? AND principal_id = ? "
                "AND revoked_at IS NULL",
                (
                    acknowledged_at,
                    ctx.username,
                    row["supersedes_id"],
                    workspace_id,
                    ctx.user_id,
                ),
            )
            await log_audit(
                db,
                action="agent_token.rotation_acknowledge",
                user=ctx.username,
                resource_type="agent_token",
                resource_id=token_id,
                details={
                    "principal_id": ctx.user_id,
                    "predecessor_id": row["supersedes_id"],
                },
                workspace_id=workspace_id,
            )
        updated = await _fetch_token(db, token_id, workspace_id)
        if updated is None:
            raise ServiceUnavailableError(
                code="token_readback_failed",
                message="The rotation acknowledgement could not be confirmed.",
            )
        response = _row_to_response(updated)
        await db.commit()
        return response
    except ServiceError:
        await db.rollback()
        raise
    except aiosqlite.Error as exc:
        await db.rollback()
        raise ServiceUnavailableError(
            code="token_store_unavailable",
            message="The token store is temporarily unavailable.",
        ) from exc
    except Exception:
        await db.rollback()
        raise


__all__ = [
    "GRAPH_INGEST_LOCAL_TOKEN_ID_PREFIX",
    "GRAPH_INGEST_SCOPE",
    "GRAPH_INGEST_TOKEN_ID_PREFIX",
    "acknowledge_rotation",
    "create_token",
    "hash_token",
    "list_tokens",
    "mint_graph_ingest_token",
    "revoke_token",
]
