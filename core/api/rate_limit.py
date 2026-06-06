# v1.0.0 - 2026-04-17 - Shared slowapi limiter instance (imported by routers)
"""Shared slowapi Limiter instance.

Routers import `limiter` from here; `api.main` also imports it here so there
is a single instance. This avoids circular imports between main.py and the
graph router.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address


def _rate_limit_key(request) -> str:  # type: ignore[no-untyped-def]
    """Per-user key when request.state.user is available, fallback to IP."""
    user = getattr(request.state, "user", None)
    if user is not None:
        user_id = getattr(user, "user_id", None)
        if user_id:
            return str(user_id)
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)
