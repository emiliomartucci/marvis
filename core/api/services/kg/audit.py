# v1.0.0 - 2026-04-16 - KG deep bundle audit + rate limiting (Phase 7.0 Commit 13)
"""KG deep bundle access audit and rate limiting.

Provides two lightweight helpers used by all endpoints that build
kg_context when deep=True:
- check_deep_rate_limit: sliding-window in-memory rate limiter
- log_kg_deep_access: structured INFO log (not DB audit — Phase 7.1)
"""
from __future__ import annotations

import collections
import logging
import time

from fastapi import HTTPException

from core.api.config import settings

logger = logging.getLogger(__name__)
_deep_rate_limits: dict[str, list[float]] = collections.defaultdict(list)


def check_deep_rate_limit(identity: str) -> None:
    """Sliding window rate limiter for deep=true KG bundle requests."""
    now = time.time()
    window = 60.0
    max_req = settings.kg_deep_rate_limit_per_min
    bucket = _deep_rate_limits[identity]
    # Evict stale entries
    _deep_rate_limits[identity] = [t for t in bucket if now - t < window]
    if len(_deep_rate_limits[identity]) >= max_req:
        raise HTTPException(
            status_code=429,
            detail=(
                f"KG deep bundle rate limit exceeded for '{identity}': "
                f"{max_req} deep requests/minute. "
                "Use deep=false for bulk list operations. "
                "Set KG_DEEP_RATE_LIMIT_PER_MIN env var to increase limit."
            ),
        )
    _deep_rate_limits[identity].append(now)


def log_kg_deep_access(user: str, endpoint: str, resource_id: str) -> None:
    """Structured INFO log for deep bundle access (DB audit deferred to Phase 7.1)."""
    logger.info(
        "kg.deep_access user=%s endpoint=%s resource=%s",
        user, endpoint, resource_id,
    )
