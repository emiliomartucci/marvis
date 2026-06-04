# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D6: extract watcher pause/resume helpers
"""KG watcher pause/resume sentinel helpers.

Extracted from `api/routers/kg.py` so the same logic can be reused by:

- the operator-facing endpoint `POST /api/v1/kg/watcher_control` (existing)
- the PR-impact populator subprocess (sub-01 D2), which must pause the
  watcher around the write window so concurrent indexing doesn't fight the
  populator's UPSERT batch
- Brain v1 future callers that need to drain the queue without writes

The sentinel is a small file at `$XDG_RUNTIME_DIR/pir-kg-watcher/paused`
(falling back to `/run/user/$UID/pir-kg-watcher/paused`). When present, the
watcher's dispatch loop drains the queue without performing DB writes.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _watcher_runtime_dir() -> Path:
    """Return the watcher runtime dir, honoring XDG_RUNTIME_DIR if set.

    Resolved on every call so tests can monkey-patch the env var without
    needing to reload the module.
    """
    return Path(
        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    ) / "pir-kg-watcher"


def _pause_sentinel_path() -> Path:
    return _watcher_runtime_dir() / "paused"


# Compat aliases for callers that imported the constants directly from
# api/routers/kg.py before the D6 extraction. New code should call the
# helpers, not these names, so the path stays resolvable at test time.
WATCHER_RUNTIME_DIR = _watcher_runtime_dir()
PAUSE_SENTINEL = _pause_sentinel_path()


class WatcherSentinelError(OSError):
    """Raised when the pause sentinel cannot be created or removed."""


def pause_watcher(duration_seconds: int | None = None) -> str | None:
    """Touch the sentinel file so the watcher drains without dispatch.

    Returns the ISO-8601 `paused_until` timestamp when `duration_seconds`
    is provided so the caller can surface it to the user. Returns None for
    indefinite pauses (operator must call `resume_watcher()` explicitly).

    The sentinel body holds the expiration timestamp so `is_paused()` can
    auto-clear stale sentinels without needing a separate watchdog. An
    empty body means indefinite.
    """
    runtime_dir = _watcher_runtime_dir()
    sentinel = runtime_dir / "paused"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise WatcherSentinelError(
            f"failed to create watcher runtime dir {runtime_dir}: {exc}"
        ) from exc

    paused_until: str | None = None
    body = ""
    if duration_seconds is not None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
        paused_until = expires_at.isoformat(timespec="seconds")
        body = paused_until

    try:
        sentinel.write_text(body)
    except OSError as exc:
        raise WatcherSentinelError(
            f"failed to write watcher pause sentinel {sentinel}: {exc}"
        ) from exc

    logger.info(
        "kg watcher paused (sentinel=%s, paused_until=%s)",
        sentinel,
        paused_until or "indefinite",
    )
    return paused_until


def resume_watcher() -> bool:
    """Remove the sentinel so the watcher resumes dispatch.

    Returns True if the sentinel existed and was removed, False if there
    was nothing to remove (idempotent).
    """
    sentinel = _pause_sentinel_path()
    if not sentinel.exists():
        return False
    try:
        sentinel.unlink()
    except OSError as exc:
        # Don't raise — the caller usually wants resume to be best-effort.
        logger.warning("kg watcher resume: rm sentinel %s failed: %s", sentinel, exc)
        return False
    logger.info("kg watcher resumed (sentinel %s removed)", sentinel)
    return True


def is_paused() -> bool:
    """Return True if the watcher should currently skip dispatch.

    Auto-clears stale sentinels (those whose ISO timestamp body has
    already elapsed) so a crashed pauser doesn't strand the watcher.
    """
    sentinel = _pause_sentinel_path()
    if not sentinel.exists():
        return False
    try:
        body = sentinel.read_text().strip()
    except OSError as exc:
        logger.warning("kg watcher is_paused: read %s failed: %s", sentinel, exc)
        return True  # fail closed — assume paused if we can't read

    if not body:
        return True  # indefinite pause

    try:
        expires_at = datetime.fromisoformat(body)
    except ValueError:
        logger.warning(
            "kg watcher is_paused: sentinel %s has malformed body %r; treating as expired",
            sentinel,
            body,
        )
        resume_watcher()
        return False

    if expires_at.tzinfo is None:
        # Defensive: previous versions may have written naive timestamps.
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expires_at:
        resume_watcher()
        return False
    return True


__all__ = [
    "WATCHER_RUNTIME_DIR",
    "PAUSE_SENTINEL",
    "WatcherSentinelError",
    "pause_watcher",
    "resume_watcher",
    "is_paused",
]
