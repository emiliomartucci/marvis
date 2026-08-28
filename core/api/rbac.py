# v1.4.0 - 2026-05-27 - S1 F0: ROLE_HIERARCHY moved to use_cases._roles (fastapi-free SoT), re-exported here
"""
RBAC -- Authorization layer.
Separato da security.py che gestisce authn (JWT, cookies, tokens).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiosqlite
from fastapi import Depends, HTTPException

from core.api.models import UserInfo
from core.api.security import (
    get_current_user,  # noqa: F401 — re-export kept for callers importing via rbac
    get_current_user_or_agent,
    get_current_user_or_delegated_agent,
)
from core.api.use_cases._roles import ROLE_HIERARCHY  # SoT lives in the fastapi-free use_cases layer

__all__ = ["ROLE_HIERARCHY", "require_role", "require_scope", "check_team_admin"]


def require_role(*allowed: str, human_only: bool = False) -> Callable[..., Awaitable[UserInfo]]:
    """FastAPI dependency factory. Raises 403 se ruolo insufficiente.

    Args:
        *allowed: Ruoli minimi richiesti (almeno uno). Usa ROLE_HIERARCHY per calcolare min_level.
        human_only: Se True, usa get_current_user_or_delegated_agent (cookie umano,
                    oppure agente con super-session delegation attiva — Constitution
                    v2.0 Rule 6; senza grant il comportamento e' identico al
                    cookie-only di prima: 401/403).
                    Se False (default), usa get_current_user_or_agent.

    Returns:
        Callable da usare come Depends() -- NON wrappato in Depends().

    IMPORTANTE: restituisce il callable diretto, non Depends(check).
    """
    if not allowed:
        raise ValueError("require_role() richiede almeno un ruolo")

    # Validazione role names -- typo silenziosamente nega tutto
    unknown = [r for r in allowed if r not in ROLE_HIERARCHY]
    if unknown:
        raise ValueError(f"require_role() ricevuto ruoli sconosciuti: {unknown}")

    min_level = min(ROLE_HIERARCHY[r] for r in allowed)
    auth_dep = (
        get_current_user_or_delegated_agent if human_only else get_current_user_or_agent
    )

    async def check(
        user: UserInfo = Depends(auth_dep),
    ) -> UserInfo:
        if ROLE_HIERARCHY.get(user.system_role, -1) < min_level:
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions",  # Generico -- non rivela topology
            )
        return user

    return check  # NON return Depends(check)


def require_scope(*required_scopes: str) -> Callable[..., Awaitable[UserInfo]]:
    """FastAPI dependency factory. Enforces agent token scopes.

    Validated human sessions and trusted local single-user calls are role-gated.
    Every bearer mechanism is scope-gated, even when its persisted owner is human.

    Usage: Depends(require_scope("read", "write"))
    """
    if not required_scopes:
        raise ValueError("require_scope() requires at least one scope")

    async def check(
        user: UserInfo = Depends(get_current_user_or_agent),
    ) -> UserInfo:
        if user.auth_mechanism in {"session", "local"} and user.user_type == "human":
            return user

        if user.auth_mechanism not in {
            "agent_token",
            "legacy_shared_token",
            "delegated_agent_token",
        }:
            raise HTTPException(
                status_code=403,
                detail="Authenticated mechanism cannot satisfy scoped access.",
            )

        # Agent with no scopes (empty/null) = NO permission for scope-gated ops.
        # Empty MUST deny, not allow-all: an unscoped agent token previously
        # passed every scope check (authz bypass — empty list satisfied
        # `not user.scopes`). Scope-gated endpoints require an explicit grant.
        if not user.scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Token missing required scopes: {list(required_scopes)}",
            )

        # Check that all required scopes are present
        missing = [s for s in required_scopes if s not in user.scopes]
        if missing:
            raise HTTPException(
                status_code=403,
                detail=f"Token missing required scopes: {missing}",
            )
        return user

    return check


async def check_team_admin(
    team_id: str,
    current_user: UserInfo,
    db: aiosqlite.Connection,
) -> bool:
    """True se l'utente e admin di questo team specifico (o admin/super_admin globale)."""
    if current_user.system_role in ("admin", "super_admin"):
        return True
    async with db.execute(
        "SELECT role FROM team_members WHERE team_id=? AND user_id=? AND role='admin'",
        [team_id, current_user.user_id]
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None
