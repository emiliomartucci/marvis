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
