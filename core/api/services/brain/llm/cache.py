"""In-memory TTL cache for Brain polish results.

Process-local dict — single FastAPI worker on `pir-api.service`. Memory leak
on stale entries is acceptable: TTL bounds the worst case to ~few hundred
small objects per hour. YAGNI vs cleanup loop.

Idempotency key shape:
    brain_{purpose}_{run_id}_{primary_id}

Underscore separator (not ":") because the Mac Gateway Idempotency-Key
header validator enforces ``^[A-Za-z0-9_-]{8,255}$`` — ":" caused HTTP 400
on every polish call so narrative_polished stayed NULL across the Wave 3.1
cycles (incident 2026-05-19).

Same (purpose, run_id, primary_id) is deterministic recompute identity —
serving polished output from the same baseline reproduces the cache hit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from core.api.services.brain.llm.base import PolishPurpose, PolishResult
from core.api.services.brain.llm.constants import DEFAULT_CACHE_TTL_SECONDS


def polish_cache_key(purpose: PolishPurpose, run_id: str, primary_id: str) -> str:
    """Stable cache key shared by router + background polish task.

    Must satisfy the Mac Gateway Idempotency-Key pattern
    ``^[A-Za-z0-9_-]{8,255}$``: alphanumerics, underscore, hyphen only.
    """
    return f"brain_{purpose}_{run_id}_{primary_id}"


@dataclass
class _CacheEntry:
    result: PolishResult
    expires_at: float


class IdempotencyCache:
    """Process-local TTL cache. Not thread-safe — single asyncio worker."""

    def __init__(self, default_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._default_ttl_seconds = max(1, int(default_ttl_seconds))

    def get(self, key: str) -> PolishResult | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._store.pop(key, None)
            return None
        return entry.result

    def set(
        self,
        key: str,
        result: PolishResult,
        ttl: int | None = None,
    ) -> None:
        ttl_value = self._default_ttl_seconds if ttl is None else max(1, int(ttl))
        self._store[key] = _CacheEntry(
            result=result,
            expires_at=time.monotonic() + ttl_value,
        )

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def size(self) -> int:
        return len(self._store)


_cache_singleton: IdempotencyCache | None = None


def get_polish_cache() -> IdempotencyCache:
    """Return the process-wide polish cache singleton."""
    global _cache_singleton
    if _cache_singleton is None:
        from core.api.config import settings

        _cache_singleton = IdempotencyCache(
            default_ttl_seconds=settings.brain_llm_polish_cache_ttl_seconds
        )
    return _cache_singleton


def reset_polish_cache() -> None:
    """Reset helper for tests."""
    global _cache_singleton
    _cache_singleton = None
