# v1.0.0 - 2026-05-27 - S1 F0: fastapi-free home for ROLE_HIERARCHY (single source of truth)
"""Role hierarchy — single source of truth, free of FastAPI.

``rbac.py`` (the HTTP authorization layer) imports ``fastapi`` at module top,
so ``use_cases`` cannot reuse ``ROLE_HIERARCHY`` from there without dragging
FastAPI into the pure domain layer. The hierarchy lives here instead and
``rbac.py`` re-exports it, keeping ONE definition shared by both layers.
"""
from __future__ import annotations

ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 0,
    "operator": 1,
    "admin": 2,
    "super_admin": 3,
}

# SSO role claim -> Marvis system_role. Capped at admin — super_admin only via
# manual seed/promotion. Shared by the HTTP SSO callback (routers/auth.py) and
# the MCP OAuth context (_adapter.py), which must not import the router.
# Sources: WorkOS org-membership roles (owner/admin/member/guest) and Entra
# app-roles (IMPL §A.0c: role values arrive already in Marvis terms — without
# these two rows an Entra `operator`/`viewer` fell to unknown → viewer).
SSO_ROLE_MAPPING: dict[str, str] = {
    "owner": "admin",
    "admin": "admin",
    "member": "operator",
    "guest": "viewer",
    "operator": "operator",
    "viewer": "viewer",
}


def map_sso_role(raw: object) -> tuple[str, bool]:
    """Map a WorkOS role claim to (system_role, known).

    Unknown or absent values fail-closed to ("viewer", False) so callers can
    distinguish "mapped" from "defaulted" (the users-row sync must never write
    a defaulted role over an existing one).
    """
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in SSO_ROLE_MAPPING:
            return SSO_ROLE_MAPPING[value], True
    return "viewer", False
