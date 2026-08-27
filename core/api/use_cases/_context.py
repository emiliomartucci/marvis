# v1.0.0 - 2026-05-27 - S1 F0: CallerContext (identity as domain data) + require_role_ctx (pure RBAC)
"""Caller context — identity as typed domain data, not a FastAPI side effect.

Every use_case takes a :class:`CallerContext` as its first parameter. Who fills
it differs per surface:

- HTTP adapter (SaaS/Enterprise/Console): :meth:`CallerContext.from_user_info`
  maps the existing ``UserInfo`` (resolved by ``Depends(get_current_user_or_agent)``
  inside the router) to a context. ``Depends`` never descends into the use_case.
- MCP local (single-user stdio): :meth:`CallerContext.local_mcp_agent` builds a
  fixed agent identity. The trusted local CLI separately uses
  :meth:`CallerContext.local_single_user`; same-shell access is not claimed as
  a cryptographic human/agent boundary. No token, no JWT.

This module is intentionally free of ``fastapi`` and ``UserInfo`` at runtime
(``UserInfo`` is imported only under ``TYPE_CHECKING`` and ``from_user_info`` is
a pure, duck-typed mapper) so the domain layer stays transport-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiosqlite

from core.api.use_cases._errors import AuthorizationError
from core.api.use_cases._roles import ROLE_HIERARCHY

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from core.api.models.auth import UserInfo


@dataclass(frozen=True)
class CallerContext:
    """Identity + role + workspace carried explicitly into every use_case."""

    username: str
    system_role: str  # viewer|operator|admin|super_admin (reuses ROLE_HIERARCHY)
    user_type: str = "human"  # human|agent
    workspace_id: str = "ws_default"
    scopes: tuple[str, ...] = field(default_factory=tuple)
    is_human_session: bool = False  # replaces the request.cookies.get("pir_session") check
    user_id: str = ""  # DB user PK; "" for local single-user / when unknown
    # Legacy audit metadata only. A caller-provided id is NEVER authorization;
    # approval authority is re-read from the persisted delegation row at the
    # use-case decision point (see ``has_approval_authority`` below).
    delegation_grant_id: str | None = None
    # Adapter-set provenance for the local single-user process. Identity claims
    # alone never turn a remote principal into the local OS account.
    local_runtime: bool = False

    @property
    def can_act_as_approver(self) -> bool:
        """Whether this is a validated human principal.

        Persisted agent delegations require a DB read and therefore deliberately
        do not participate in this local property. In particular, a synthetic
        ``delegation_grant_id`` string never grants authority.
        """
        return self.user_type == "human" and self.is_human_session

    @property
    def is_local_os_account(self) -> bool:
        """Whether the caller is the explicit local single-user OS account.

        This is a data/filesystem visibility boundary, not approval authority.
        Both the trusted local CLI and local stdio MCP operate as this account;
        only :attr:`can_act_as_approver` distinguishes human approval authority.
        """
        return (
            self.local_runtime
            and self.username == "local"
            and self.user_id == "local"
            and self.workspace_id == "ws_default"
        )

    @classmethod
    def local_single_user(cls) -> "CallerContext":
        """Trusted local CLI/loopback identity for the current OS account."""
        return cls(
            username="local",
            system_role="operator",
            user_type="human",
            is_human_session=True,
            user_id="local",
            local_runtime=True,
        )

    @classmethod
    def local_mcp_agent(cls) -> "CallerContext":
        """Local stdio MCP identity: same OS account, but agent authority.

        A process with the same shell authority can invoke the trusted CLI, so
        this is not a cryptographic separation from the human.  It does keep
        MCP/automation calls agentic inside the domain layer and prevents them
        from satisfying human-only approval checks by merely selecting stdio.
        """
        return cls(
            username="local",
            system_role="operator",
            user_type="agent",
            is_human_session=False,
            user_id="local",
            local_runtime=True,
        )

    @classmethod
    def from_user_info(
        cls,
        u: "UserInfo",
        *,
        is_human_session: bool,
        delegation_grant_id: str | None = None,
    ) -> "CallerContext":
        """Pure adapter mapper: existing ``UserInfo`` -> ``CallerContext``.

        Duck-typed (no runtime ``UserInfo`` import) and side-effect free. Maps the
        fields ``CallerContext`` actually carries; ``UserInfo.teams`` has no
        counterpart here and is intentionally dropped. ``delegation_grant_id``
        is retained only for backwards-compatible audit metadata and is never
        treated as authorization.
        """
        return cls(
            username=u.username,
            system_role=u.system_role,
            user_type=u.user_type,
            workspace_id=u.workspace_id,
            scopes=tuple(u.scopes),
            is_human_session=is_human_session,
            user_id=u.user_id,
            delegation_grant_id=delegation_grant_id,
            local_runtime=getattr(u, "auth_mechanism", "unknown") == "local",
        )


@dataclass(frozen=True)
class ApprovalAuthorityReceipt:
    """Evidence for the principal that opened a privileged approval gate."""

    kind: str
    grant_id: str | None = None
    granted_by: str | None = None
    granted_by_role: str | None = None

    def audit_details(self) -> dict[str, str]:
        details = {"authority_kind": self.kind}
        if self.grant_id is not None:
            details["delegation_grant_id"] = self.grant_id
        if self.granted_by is not None:
            details["delegation_granted_by"] = self.granted_by
        if self.granted_by_role is not None:
            details["delegation_granted_by_role"] = self.granted_by_role
        return details


async def find_active_delegation(
    agent_username: str,
    workspace_id: str,
    db: aiosqlite.Connection,
    *,
    minimum_role: str | None = None,
) -> aiosqlite.Row | None:
    """Return a persisted, live, bounded delegation for ``agent_username``.

    The only currently issued scope is ``full``. Unknown/future scopes fail
    closed until a use case explicitly understands their bounds. A missing
    migration also fails closed, which keeps pure unit-test schemas and partial
    upgrades from accidentally authorizing an agent.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with db.execute(
            "SELECT id, granted_by, granted_by_user_id, granted_by_role, scope, "
            "created_at, expires_at "
            "FROM delegations "
            "WHERE agent_username = ? AND workspace_id = ? AND scope = 'full' "
            "AND revoked_at IS NULL AND created_at <= ? AND expires_at > ? "
            "ORDER BY expires_at DESC",
            (agent_username, workspace_id, now, now),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.Error:
        return None

    minimum_level = ROLE_HIERARCHY[minimum_role] if minimum_role else None
    for row in rows:
        try:
            created_at = datetime.fromisoformat(row["created_at"])
            expires_at = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            continue
        try:
            lifetime = expires_at - created_at
        except TypeError:
            continue
        if not timedelta(0) < lifetime <= timedelta(days=7):
            continue
        if minimum_level is not None and ROLE_HIERARCHY.get(
            row["granted_by_role"], -1
        ) < minimum_level:
            continue
        return row
    return None


async def has_approval_authority(
    ctx: CallerContext, db: aiosqlite.Connection
) -> bool:
    """Validated human/local principal or live operator+ delegation."""
    return await resolve_approval_authority(ctx, db) is not None


async def resolve_approval_authority(
    ctx: CallerContext, db: aiosqlite.Connection
) -> ApprovalAuthorityReceipt | None:
    """Return persisted authority evidence, never a caller-provided grant id."""
    if ctx.can_act_as_approver:
        return ApprovalAuthorityReceipt(kind="human_session")
    if ctx.user_type != "agent":
        return None
    workspace_id = require_workspace_ctx(ctx)
    grant = await find_active_delegation(
        ctx.username,
        workspace_id,
        db,
        minimum_role="operator",
    )
    if grant is None:
        return None
    return ApprovalAuthorityReceipt(
        kind="persisted_delegation",
        grant_id=grant["id"],
        granted_by=grant["granted_by"],
        granted_by_role=grant["granted_by_role"],
    )


def require_role_ctx(ctx: CallerContext, *allowed: str) -> None:
    """Pure equivalent of ``rbac.require_role``: raises ``AuthorizationError``.

    Raises if ``ctx.system_role``'s level in ``ROLE_HIERARCHY`` is below the
    minimum required level among ``allowed``. This is a plain imperative function,
    NOT a FastAPI dependency — it never returns ``Depends(...)``.
    """
    if not allowed:
        raise ValueError("require_role_ctx() requires at least one role")
    unknown = [r for r in allowed if r not in ROLE_HIERARCHY]
    if unknown:
        raise ValueError(f"require_role_ctx() received unknown roles: {unknown}")

    min_level = min(ROLE_HIERARCHY[r] for r in allowed)
    if ROLE_HIERARCHY.get(ctx.system_role, -1) < min_level:
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )


def require_workspace_ctx(ctx: CallerContext) -> str:
    """Return the authenticated workspace or fail closed when it is missing.

    ``ws_default`` remains the explicit OSS/local-single-user workspace.  It is
    not inferred for hosted identities: adapters must carry the workspace that
    was authenticated for the principal.
    """
    workspace_id = (ctx.workspace_id or "").strip()
    if workspace_id:
        return workspace_id
    if ctx.user_id == "local" and ctx.username == "local":
        return "ws_default"
    raise AuthorizationError(
        code="workspace_context_required",
        message="Authenticated workspace context is required",
    )
