# v1.0.0 - 2026-05-27 - S1 F0: CallerContext (identity as domain data) + require_role_ctx (pure RBAC)
"""Caller context — identity as typed domain data, not a FastAPI side effect.

Every use_case takes a :class:`CallerContext` as its first parameter. Who fills
it differs per surface:

- HTTP adapter (SaaS/Enterprise/Console): :meth:`CallerContext.from_user_info`
  maps the existing ``UserInfo`` (resolved by ``Depends(get_current_user_or_agent)``
  inside the router) to a context. ``Depends`` never descends into the use_case.
- MCP local (OSS single-user): :meth:`CallerContext.local_single_user` builds a
  default local identity. No token, no JWT.

This module is intentionally free of ``fastapi`` and ``UserInfo`` at runtime
(``UserInfo`` is imported only under ``TYPE_CHECKING`` and ``from_user_info`` is
a pure, duck-typed mapper) so the domain layer stays transport-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
    # Super-session (Constitution v2.0 Rule 6): id of the active delegation the
    # HTTP adapter resolved for an agent caller; None when no grant applies.
    delegation_grant_id: str | None = None

    @property
    def can_act_as_approver(self) -> bool:
        """Human session OR an active super-session delegation (Rule 6)."""
        return self.is_human_session or self.delegation_grant_id is not None

    @classmethod
    def local_single_user(cls) -> "CallerContext":
        """Default identity for OSS single-user MCP: local operator, human session."""
        return cls(
            username="local",
            system_role="operator",
            user_type="human",
            is_human_session=True,
            user_id="local",
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
        is filled by the HTTP adapter when the caller is an agent with an active
        super-session delegation (Constitution v2.0 Rule 6).
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
