# v1.0.0 - 2026-09-21 - Plan 2026-09-21 KTD3: live-read flag files for the Jev provider.
"""Live-read enablement flags for the Jev provider (plan KTD3).

Why flag FILES and not env/config: pydantic settings and the systemd
``EnvironmentFile`` are a boot-time snapshot — a flip could never reach a
running unit, and "rollback = flip, never restart" would be impossible.
These flags are small files on disk whose EXISTENCE means ON; each read is
cached for at most ``cache_seconds`` (default 60s, plan KTD3) and refreshed
when the file mtime changes.

Precedence (plan KTD3): global kill-switch file > tenant flag file.
The kill file exists = the whole provider is OFF for every tenant.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FlagDecision:
    enabled: bool
    reason: str  # "global_kill" | "tenant_off" | "tenant_on"


class _PathCache:
    """Per-path existence cache bounded by mtime + TTL."""

    def __init__(self, cache_seconds: float) -> None:
        self._cache_seconds = max(0.0, cache_seconds)
        self._entries: dict[str, tuple[float, bool, float]] = {}  # path -> (ts, exists, mtime)

    def exists(self, path: str) -> bool:
        try:
            stat = os.stat(path)
            mtime, now = stat.st_mtime, time.monotonic()
        except FileNotFoundError:
            mtime, now = -1.0, time.monotonic()

        cached = self._entries.get(path)
        if cached is not None:
            ts, exists, cached_mtime = cached
            fresh = (now - ts) < self._cache_seconds and cached_mtime == mtime
            if fresh:
                return exists
        exists = mtime >= 0.0
        self._entries[path] = (now, exists, mtime)
        return exists

    def reset(self) -> None:
        self._entries.clear()


class FlagResolver:
    """Evaluates global kill-switch and per-tenant flag with one precedence."""

    def __init__(
        self,
        *,
        global_kill_file: str | os.PathLike[str],
        tenant_flags_root: str | os.PathLike[str],
        tenant_flag_name: str = "jev.flag",
        cache_seconds: float = 60.0,
    ) -> None:
        self._global_kill_file = str(global_kill_file)
        self._tenant_flags_root = Path(tenant_flags_root)
        self._tenant_flag_name = tenant_flag_name
        self._cache = _PathCache(cache_seconds)

    def decide(self, tenant_id: str | None) -> FlagDecision:
        try:
            return self._decide(tenant_id)
        except OSError:
            # An unreadable kill switch is not evidence that it is absent.
            return FlagDecision(False, "unreadable_flags")

    def _decide(self, tenant_id: str | None) -> FlagDecision:
        if self._cache.exists(self._global_kill_file):
            return FlagDecision(False, "global_kill")
        tenant = (tenant_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", tenant):
            return FlagDecision(False, "tenant_off")
        flag_path = self._tenant_flags_root / tenant / self._tenant_flag_name
        if self._cache.exists(str(flag_path)):
            return FlagDecision(True, "tenant_on")
        return FlagDecision(False, "tenant_off")

    def reset_cache(self) -> None:
        self._cache.reset()


_resolver: FlagResolver | None = None


def _settings():
    from core.api import config as _config

    return _config.settings


def get_flag_resolver() -> FlagResolver:
    """Process-wide resolver built from settings (late-bind, test-friendly)."""
    global _resolver
    if _resolver is None:
        settings = _settings()
        _resolver = FlagResolver(
            global_kill_file=settings.typesafe_global_kill_file,
            tenant_flags_root=settings.typesafe_tenant_flags_root,
            cache_seconds=settings.typesafe_flag_cache_seconds,
        )
    return _resolver


def reset_flag_resolver() -> None:
    """Test hook: drop the cached resolver and its path caches."""
    global _resolver
    _resolver = None


__all__ = [
    "FlagDecision",
    "FlagResolver",
    "get_flag_resolver",
    "reset_flag_resolver",
]
