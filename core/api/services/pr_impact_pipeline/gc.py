# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D8: webhook_deliveries retention sweep
"""Daily GC for `webhook_deliveries`.

The webhook handler INSERTs one row per inbound delivery (D3), which grows
indefinitely. This module trims rows older than `retention_days` so the
table doesn't bloat the SQLite WAL.

Rows in `pending`, `failed`, or `dead` are kept indefinitely — they
represent live or unresolved deliveries that the admin DLQ replay (D5)
might still need. We only sweep `processed` and `skipped` rows.

Sleep-FIRST per learning `4d4278e4`.
"""
from __future__ import annotations

import asyncio
import logging

import aiosqlite

logger = logging.getLogger(__name__)


_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_SWEEP_INTERVAL = 86400  # 24h
_SWEEPABLE_STATUSES = ("processed", "skipped")


async def sweep_webhook_deliveries(
    db_path: str,
    *,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> int:
    """Delete `processed`/`skipped` rows older than `retention_days`.

    Returns the number of rows deleted. Idempotent — running it twice in
    a row only deletes new rows that crossed the cutoff in between.
    """
    if retention_days <= 0:
        raise ValueError("retention_days must be > 0")
    cutoff_clause = f"-{retention_days} days"
    placeholders = ",".join("?" for _ in _SWEEPABLE_STATUSES)
    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        cursor = await db.execute(
            f"""
            DELETE FROM webhook_deliveries
             WHERE status IN ({placeholders})
               AND received_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
            """,
            (*_SWEEPABLE_STATUSES, cutoff_clause),
        )
        await db.commit()
    deleted = cursor.rowcount or 0
    if deleted:
        logger.info(
            "sweep_webhook_deliveries deleted %d row(s) older than %d days",
            deleted,
            retention_days,
        )
    return deleted


async def periodic_webhook_gc(
    db_path: str,
    *,
    interval_seconds: int = _DEFAULT_SWEEP_INTERVAL,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> None:
    """Background coroutine that runs `sweep_webhook_deliveries` daily.

    Sleep-FIRST so a crashed pauser cannot block this coroutine from
    obtaining the write lock at API startup.
    """
    logger.info(
        "periodic_webhook_gc started (interval=%ds, retention=%dd)",
        interval_seconds,
        retention_days,
    )
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await sweep_webhook_deliveries(db_path, retention_days=retention_days)
        except asyncio.CancelledError:
            logger.info("periodic_webhook_gc cancelled — exiting")
            return
        except Exception as exc:  # noqa: BLE001 — must not crash the loop
            logger.exception("periodic_webhook_gc tick failed: %s", exc)


__all__ = [
    "sweep_webhook_deliveries",
    "periodic_webhook_gc",
]
