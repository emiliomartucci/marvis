"""User provisioning queue (RBAC F3, plan d33292f0).

FastAPI-free lifecycle for ``user_provisioning_queue``: operators enqueue
add_user requests; the root provisioner worker (outside this process — the
tenant never sees WORKOS_API_KEY) reads the pending batch and reports the
outcome through the /internal loopback endpoints. This module owns every DB
transition; the worker owns only the WorkOS side effects.

States: queued -> done | failed | rejected. No lease: the worker is a systemd
oneshot timer (no overlap); a crash mid-item leaves the row queued and the
idempotent WorkOS branches retry it. ``attempts`` increments only on a
reported error; 3 = poison (failed + admin notification).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid

import aiosqlite

from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import ServiceError
from core.api.use_cases._roles import ROLE_HIERARCHY

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_MINTABLE_ROLES = {"operator", "viewer"}
_POISON_ATTEMPTS = 3
_STALE_SWEEP_HOURS = 24

FIRST_LOGIN_INSTRUCTIONS = (
    "Utente in provisioning (~60s). Primo accesso: il collega apre il connettore "
    "MCP del tenant e si autentica con la sua email via Magic Auth (codice a 6 "
    "cifre via email, controllare lo spam su M365) oppure Google. Finché il "
    "provisioning non è completato il login fallisce con un errore di "
    "organizzazione: è la race attesa, riprovare dopo un minuto. Stato: "
    "list_user_requests."
)


class UserProvisioningError(ServiceError):
    """Domain error for the user provisioning queue."""


def _rank(role: str | None) -> int:
    return ROLE_HIERARCHY.get((role or "").strip(), -1)


def _actor_is_service_admin(actor: CallerContext) -> bool:
    # The static tenant bearer maps to user_id "tenant:<slug>" with admin role.
    return actor.system_role in {"admin", "super_admin"}


def _row_dict(row: aiosqlite.Row) -> dict:
    data = {key: row[key] for key in row.keys()}
    if data.get("teams_json"):
        try:
            data["teams"] = json.loads(data["teams_json"])
        except (TypeError, ValueError):
            data["teams"] = []
    else:
        data["teams"] = []
    data.pop("teams_json", None)
    return data


async def _requester_rank(
    db: aiosqlite.Connection,
    requester_id: str,
    workspace_id: str,
) -> int:
    """Current rank of a requester. Service identities count as admin."""
    if requester_id.startswith("tenant:"):
        return ROLE_HIERARCHY["admin"]
    cur = await db.execute(
        "SELECT system_role FROM users "
        "WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL LIMIT 1",
        (requester_id, workspace_id),
    )
    row = await cur.fetchone()
    return _rank(str(row[0])) if row is not None else -1


async def _audit(
    db: aiosqlite.Connection,
    *,
    action: str,
    actor_id: str,
    request_id: str,
    details: dict,
    workspace_id: str,
) -> None:
    """Append required, workspace-bound evidence inside the caller transaction."""
    from core.api.services.audit import log_audit

    await log_audit(
        db,
        action=f"user_provisioning.{action}",
        user=actor_id,
        resource_type="user_provisioning_request",
        resource_id=request_id,
        details=details,
        workspace_id=workspace_id,
    )


async def enqueue_request(
    db: aiosqlite.Connection,
    actor: CallerContext,
    *,
    email: str,
    role: str,
    teams: list[str] | None = None,
) -> dict:
    """Queue an add_user request. Operator+ with a mint ceiling ≤ own rank."""
    if _rank(actor.system_role) < ROLE_HIERARCHY["operator"]:
        raise UserProvisioningError(code="scope_denied", message="add_user requires operator role or above")
    workspace_id = require_workspace_ctx(actor)

    requested = (role or "").strip().lower()
    if requested not in _MINTABLE_ROLES:
        # Admins are created only through the admin-only console path.
        raise UserProvisioningError(code="invalid_role", message="requested role must be operator or viewer")
    if _rank(requested) > _rank(actor.system_role):
        raise UserProvisioningError(code="scope_denied", message="cannot mint a role above your own")

    normalized_email = (email or "").strip().lower()
    if not _EMAIL_RE.fullmatch(normalized_email):
        raise UserProvisioningError(code="invalid_email", message="email is not valid")

    cur = await db.execute(
        "SELECT id FROM users "
        "WHERE lower(email) = ? AND workspace_id = ? AND deleted_at IS NULL LIMIT 1",
        (normalized_email, workspace_id),
    )
    if await cur.fetchone() is not None:
        raise UserProvisioningError(code="already_member", message="this email is already a tenant member — use grant_access/teams")

    team_ids = [t.strip() for t in (teams or []) if t and t.strip()]
    for team_id in team_ids:
        cur = await db.execute(
            "SELECT id FROM teams "
            "WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL LIMIT 1",
            (team_id, workspace_id),
        )
        if await cur.fetchone() is None:
            raise UserProvisioningError(code="team_not_found", message=f"team not found: {team_id}")
        if not _actor_is_service_admin(actor):
            cur = await db.execute(
                "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ? AND role = 'admin' LIMIT 1",
                (team_id, actor.user_id),
            )
            if await cur.fetchone() is None:
                raise UserProvisioningError(
                    code="scope_denied", message=f"only the team lead or an org-admin can pre-assign team {team_id}"
                )

    request_id = str(uuid.uuid4())
    requester = actor.user_id or actor.username
    try:
        await db.execute(
            "INSERT INTO user_provisioning_queue "
            "(id, email, requested_role, teams_json, requester_id, workspace_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                request_id,
                normalized_email,
                requested,
                json.dumps(team_ids) if team_ids else None,
                requester,
                workspace_id,
            ),
        )
    except aiosqlite.IntegrityError:
        cur = await db.execute(
            "SELECT * FROM user_provisioning_queue "
            "WHERE workspace_id = ? AND email = ? AND status = 'queued' LIMIT 1",
            (workspace_id, normalized_email),
        )
        existing = await cur.fetchone()
        await db.commit()
        result = _row_dict(existing) if existing is not None else {"email": normalized_email}
        result["notice"] = "already_queued"
        result["message"] = "una richiesta per questa email è già in coda"
        return result
    await _audit(
        db,
        action="request",
        actor_id=requester,
        request_id=request_id,
        details={"role": requested, "team_count": len(team_ids)},
        workspace_id=workspace_id,
    )
    await db.commit()

    cur = await db.execute(
        "SELECT * FROM user_provisioning_queue WHERE id = ? AND workspace_id = ?",
        (request_id, workspace_id),
    )
    row = await cur.fetchone()
    result = _row_dict(row)
    result["message"] = FIRST_LOGIN_INSTRUCTIONS
    return result


async def list_requests(db: aiosqlite.Connection, actor: CallerContext, *, limit: int = 50) -> list[dict]:
    """Requests visible to the actor: own rows, everything for admins."""
    if _rank(actor.system_role) < ROLE_HIERARCHY["operator"]:
        raise UserProvisioningError(code="scope_denied", message="list_user_requests requires operator role or above")
    workspace_id = require_workspace_ctx(actor)
    if _actor_is_service_admin(actor):
        cur = await db.execute(
            "SELECT * FROM user_provisioning_queue "
            "WHERE workspace_id = ? ORDER BY created_at DESC LIMIT ?",
            (workspace_id, limit),
        )
    else:
        cur = await db.execute(
            "SELECT * FROM user_provisioning_queue "
            "WHERE workspace_id = ? AND requester_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (workspace_id, actor.user_id or actor.username, limit),
        )
    return [_row_dict(row) for row in await cur.fetchall()]


async def pending_batch(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    limit: int = 20,
) -> list[dict]:
    """Worker-facing batch. Tenant-side hygiene runs here (single writer):

    1. sweep: poisoned-and-stale queued rows (attempts ≥ 3, older than 24h)
       flip to failed — all that remains of reconciliation in v1;
    2. requester re-check: rows whose requester is gone or below operator (or
       below the requested role) flip to rejected (offboarding path).
    """
    await db.execute(
        f"""
        UPDATE user_provisioning_queue
        SET status = 'failed', processed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
            error = COALESCE(error, 'stale: attempts exhausted')
        WHERE status = 'queued' AND attempts >= {_POISON_ATTEMPTS}
          AND workspace_id = ?
          AND created_at < strftime('%Y-%m-%dT%H:%M:%fZ','now', '-{_STALE_SWEEP_HOURS} hours')
        """,
        (workspace_id,),
    )
    cur = await db.execute(
        "SELECT * FROM user_provisioning_queue "
        "WHERE workspace_id = ? AND status = 'queued' ORDER BY created_at LIMIT ?",
        (workspace_id, limit),
    )
    rows = [_row_dict(row) for row in await cur.fetchall()]
    ready: list[dict] = []
    for row in rows:
        requester_rank = await _requester_rank(
            db,
            str(row["requester_id"]),
            workspace_id,
        )
        if requester_rank < ROLE_HIERARCHY["operator"] or requester_rank < _rank(str(row["requested_role"])):
            await db.execute(
                "UPDATE user_provisioning_queue SET status = 'rejected', "
                "processed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "error = 'requester no longer eligible' "
                "WHERE id = ? AND workspace_id = ? AND status = 'queued'",
                (row["id"], workspace_id),
            )
            await _audit(db, action="rejected", actor_id="system", request_id=str(row["id"]),
                         details={"reason": "requester no longer eligible"},
                         workspace_id=workspace_id)
            continue
        row["requester_role_rank"] = requester_rank
        ready.append(row)
    await db.commit()
    return ready


async def _notify_admins_poison(
    db: aiosqlite.Connection,
    row: dict,
    workspace_id: str,
) -> None:
    """Best-effort poison notification for tenant admins via the single-writer notify().

    Routes through ``notification_service.notify()`` (P1 F1) instead of a raw INSERT.
    The ``user_provisioning_request`` target_type is legalized by migration 163 — on a
    pre-163 DB the CHECK still rejects it, but notify() LOGS the failure per recipient
    (never swallows silently) and the queue is never blocked.
    """
    from core.api.services.notification_service import notify

    try:
        cur = await db.execute(
            "SELECT id FROM users WHERE workspace_id = ? "
            "AND system_role IN ('admin','super_admin') AND deleted_at IS NULL",
            (workspace_id,),
        )
        admins = [str(r[0]) for r in await cur.fetchall()]
        await notify(
            db,
            user_ids=admins,
            type="user_provisioning_failed",
            title="Provisioning utente fallito",
            body=(
                f"add_user per {row.get('email')} fallito dopo "
                f"{row.get('attempts')} tentativi: {row.get('error')}"
            ),
            target_type="user_provisioning_request",
            target_id=str(row.get("id")),
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001 — never block the queue on notification shape drift
        logger.warning("poison notification failed (non-critical): %s", exc)


async def _requested_active_teams(
    db: aiosqlite.Connection,
    row: dict,
    workspace_id: str,
) -> list[str]:
    """Return distinct requested teams only after proving current ownership."""
    requested = row.get("teams") or []
    if not isinstance(requested, list):
        raise UserProvisioningError(
            code="team_unavailable",
            message="requested team set is invalid",
        )
    team_ids = list(
        dict.fromkeys(
            str(team_id).strip()
            for team_id in requested
            if str(team_id).strip()
        )
    )
    for team_id in team_ids:
        cur = await db.execute(
            "SELECT 1 FROM teams WHERE id = ? AND workspace_id = ? "
            "AND deleted_at IS NULL LIMIT 1",
            (team_id, workspace_id),
        )
        if await cur.fetchone() is None:
            raise UserProvisioningError(
                code="team_unavailable",
                message=f"requested team is no longer active: {team_id}",
            )
    return team_ids


async def _available_workos_slug(
    db: aiosqlite.Connection,
    *,
    email: str,
    workos_user_id: str,
    workspace_id: str,
) -> str:
    """Preserve the legacy email slug when free, otherwise bind it to tenant+id."""
    local_part = email.split("@", 1)[0].lower()
    legacy_slug = re.sub(r"[^a-z0-9_-]+", "-", local_part).strip("-")[:50]
    if legacy_slug:
        cur = await db.execute("SELECT 1 FROM users WHERE slug = ?", (legacy_slug,))
        if await cur.fetchone() is None:
            return legacy_slug

    fallback = "workos-" + hashlib.sha256(
        f"{workspace_id}\0{workos_user_id}".encode("utf-8")
    ).hexdigest()[:16]
    cur = await db.execute("SELECT 1 FROM users WHERE slug = ?", (fallback,))
    if await cur.fetchone() is not None:
        raise UserProvisioningError(
            code="workos_user_identity_conflict",
            message="WorkOS user identity collides with an existing user",
        )
    return fallback


def _assert_exact_workos_user(
    user: aiosqlite.Row | None,
    *,
    email: str,
    role: str,
    workos_user_id: str,
    workspace_id: str,
) -> None:
    """Fail closed unless readback proves the exact external tenant identity."""
    if user is None:
        raise UserProvisioningError(
            code="workos_user_not_persisted",
            message="WorkOS user could not be persisted",
        )
    if str(user["workspace_id"] or "") != workspace_id:
        raise UserProvisioningError(
            code="workos_user_workspace_conflict",
            message="WorkOS user belongs to a different workspace",
        )
    if user["deleted_at"] is not None:
        raise UserProvisioningError(
            code="workos_user_inactive",
            message="WorkOS user is inactive and requires explicit restoration",
        )
    if not (
        str(user["id"]) == workos_user_id
        and str(user["auth_provider"] or "") == "workos"
        and str(user["external_id"] or "") == workos_user_id
        and str(user["email"] or "").lower() == email
        and str(user["type"] or "") == "human"
        and str(user["system_role"] or "") == role
        and bool(str(user["slug"] or "").strip())
    ):
        raise UserProvisioningError(
            code="workos_user_identity_conflict",
            message="WorkOS user ID is already bound to a different identity",
        )


async def complete_request(
    db: aiosqlite.Connection,
    *,
    request_id: str,
    outcome: str,
    workos_user_id: str | None = None,
    error: str | None = None,
    workspace_id: str,
) -> dict:
    """Record the worker's outcome. Payload is outcome-only by design: email,
    role and teams are read EXCLUSIVELY from the queue row (a compromised
    worker request cannot rewrite the target). Replay is a no-op."""
    outcome = (outcome or "").strip().lower()
    if outcome not in {"done", "error", "rejected"}:
        raise UserProvisioningError(code="invalid_outcome", message="outcome must be done, error, or rejected")

    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        raise UserProvisioningError(
            code="workspace_required",
            message="workspace_id is required",
        )
    cur = await db.execute(
        "SELECT * FROM user_provisioning_queue WHERE id = ? AND workspace_id = ?",
        (request_id, workspace_id),
    )
    raw = await cur.fetchone()
    if raw is None:
        raise UserProvisioningError(code="request_not_found", message="unknown provisioning request")
    row = _row_dict(raw)
    if row["status"] != "queued":
        return {"id": request_id, "status": row["status"], "replay": True}

    if outcome == "rejected":
        await db.execute(
            "UPDATE user_provisioning_queue SET status = 'rejected', "
            "processed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), error = ? "
            "WHERE id = ? AND workspace_id = ?",
            (error, request_id, workspace_id),
        )
        await _audit(
            db,
            action="rejected",
            actor_id="worker",
            request_id=request_id,
            details={"has_error": bool(error)},
            workspace_id=workspace_id,
        )
        await db.commit()
        return {"id": request_id, "status": "rejected"}

    if outcome == "error":
        attempts = int(row["attempts"]) + 1
        poisoned = attempts >= _POISON_ATTEMPTS
        await db.execute(
            "UPDATE user_provisioning_queue SET attempts = ?, error = ?, "
            "status = CASE WHEN ? THEN 'failed' ELSE status END, "
            "processed_at = CASE WHEN ? THEN "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE processed_at END "
            "WHERE id = ? AND workspace_id = ?",
            (attempts, error, poisoned, poisoned, request_id, workspace_id),
        )
        await _audit(db, action="error", actor_id="worker", request_id=request_id,
                     details={"attempts": attempts, "has_error": bool(error), "poisoned": poisoned},
                     workspace_id=workspace_id)
        if poisoned:
            row.update(attempts=attempts, error=error)
            await _notify_admins_poison(db, row, workspace_id)
        await db.commit()
        return {"id": request_id, "status": "failed" if poisoned else "queued", "attempts": attempts}

    # outcome == done
    if not workos_user_id or not workos_user_id.strip():
        raise UserProvisioningError(code="workos_user_id_required", message="done outcome requires workos_user_id")
    workos_user_id = workos_user_id.strip()

    # Re-check the requester in the queued->complete window (security #4).
    requester_rank = await _requester_rank(
        db,
        str(row["requester_id"]),
        workspace_id,
    )
    if requester_rank < ROLE_HIERARCHY["operator"] or requester_rank < _rank(str(row["requested_role"])):
        await db.execute(
            "UPDATE user_provisioning_queue SET status = 'rejected', "
            "processed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "error = 'requester no longer eligible at completion' "
            "WHERE id = ? AND workspace_id = ?",
            (request_id, workspace_id),
        )
        await _audit(db, action="rejected", actor_id="worker", request_id=request_id,
                     details={"reason": "requester no longer eligible at completion"},
                     workspace_id=workspace_id)
        await db.commit()
        return {"id": request_id, "status": "rejected"}

    email = str(row["email"])
    requested_role = str(row["requested_role"])
    team_ids = await _requested_active_teams(db, row, workspace_id)

    cur = await db.execute(
        "SELECT id,slug,email,system_role,type,auth_provider,external_id,"
        "workspace_id,deleted_at FROM users WHERE id = ? LIMIT 1",
        (workos_user_id,),
    )
    exact_user = await cur.fetchone()
    if exact_user is not None:
        _assert_exact_workos_user(
            exact_user,
            email=email,
            role=requested_role,
            workos_user_id=workos_user_id,
            workspace_id=workspace_id,
        )
    else:
        slug = await _available_workos_slug(
            db,
            email=email,
            workos_user_id=workos_user_id,
            workspace_id=workspace_id,
        )
        try:
            await db.execute(
                "INSERT INTO users "
                "(id,slug,display_name,email,system_role,type,auth_provider,"
                "external_id,workspace_id) "
                "VALUES (?, ?, ?, ?, ?, 'human', 'workos', ?, ?)",
                (
                    workos_user_id,
                    slug,
                    email,
                    email,
                    requested_role,
                    workos_user_id,
                    workspace_id,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise UserProvisioningError(
                code="workos_user_identity_conflict",
                message="WorkOS user identity collides with an existing user",
            ) from exc

    cur = await db.execute(
        "SELECT id,slug,email,system_role,type,auth_provider,external_id,"
        "workspace_id,deleted_at FROM users WHERE id = ? LIMIT 1",
        (workos_user_id,),
    )
    _assert_exact_workos_user(
        await cur.fetchone(),
        email=email,
        role=requested_role,
        workos_user_id=workos_user_id,
        workspace_id=workspace_id,
    )

    # One writer transaction: exact user + proven memberships + queue transition.
    for team_id in team_ids:
        await db.execute(
            "INSERT OR IGNORE INTO team_members (team_id, user_id, role, is_admin) "
            "SELECT id, ?, 'member', 0 FROM teams "
            "WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
            (workos_user_id, team_id, workspace_id),
        )
        membership = await db.execute(
            "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, workos_user_id),
        )
        if await membership.fetchone() is None:
            raise UserProvisioningError(
                code="team_unavailable",
                message=f"requested team could not be assigned: {team_id}",
            )
    updated = await db.execute(
        "UPDATE user_provisioning_queue SET status = 'done', workos_user_id = ?, "
        "processed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), error = NULL "
        "WHERE id = ? AND workspace_id = ? AND status = 'queued'",
        (workos_user_id, request_id, workspace_id),
    )
    if updated.rowcount != 1:
        raise UserProvisioningError(
            code="request_state_conflict",
            message="provisioning request changed during completion",
        )
    await _audit(db, action="done", actor_id="worker", request_id=request_id,
                 details={"workos_user_id_recorded": True,
                          "role": requested_role,
                          "team_count": len(team_ids)},
                 workspace_id=workspace_id)
    await db.commit()
    return {"id": request_id, "status": "done", "workos_user_id": workos_user_id}
