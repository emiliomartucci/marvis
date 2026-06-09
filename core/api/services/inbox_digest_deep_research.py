from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from core.api.services.inbox_tldr import get_or_generate_deep_research

logger = logging.getLogger(__name__)

# Serializes the on-read trigger and the periodic scheduler so two sweeps never
# overlap and hammer the gateway for the same items.
_sweep_lock = asyncio.Lock()


async def precompute_deep_research_for_items(
    *,
    inbox_item_ids: list[str],
    workspace_id: str,
    cycle_key: str,
    concurrency: int = 1,
    allow_cloud_fallback: bool = False,
) -> dict[str, Any]:
    """Best-effort Deep Analysis precompute for a prepared digest cycle."""
    stats: dict[str, Any] = {
        "cycle_key": cycle_key,
        "attempted": 0,
        "generated": 0,
        "cached": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }
    item_ids = [item_id for item_id in inbox_item_ids if item_id]
    stats["skipped"] = len(inbox_item_ids) - len(item_ids)
    if not item_ids:
        return stats

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def precompute_one(item_id: str) -> None:
        async with semaphore:
            stats["attempted"] += 1
            try:
                result = await get_or_generate_deep_research(
                    item_id,
                    workspace_id,
                    force=False,
                    allow_cloud_fallback=allow_cloud_fallback,
                )
            except HTTPException as exc:
                stats["failed"] += 1
                stats["errors"].append(
                    {"item_id": item_id, "status_code": exc.status_code}
                )
                logger.warning(
                    "Digest Deep Analysis precompute failed for %s cycle=%s status=%s",
                    item_id,
                    cycle_key,
                    exc.status_code,
                )
                return
            except Exception as exc:
                stats["failed"] += 1
                stats["errors"].append(
                    {"item_id": item_id, "error": type(exc).__name__}
                )
                logger.warning(
                    "Digest Deep Analysis precompute failed for %s cycle=%s: %s",
                    item_id,
                    cycle_key,
                    exc,
                )
                return

            if result.get("cached"):
                stats["cached"] += 1
            else:
                stats["generated"] += 1

    await asyncio.gather(*(precompute_one(item_id) for item_id in item_ids))
    return stats


async def sweep_visible_missing_deep_research(
    *,
    workspace_id: str = "ws_default",
    limit: int = 50,
) -> dict[str, Any]:
    """Deepen any current-cycle VISIBLE digest item still missing deep research.

    Enforces the invariant "an item shown in the daily visible digest must be
    deepened". Catches both 6 UTC precompute failures (gateway slow/down, no
    retry) and items promoted into the visible set later in the day. Idempotent
    (only targets rows where deep_research is NULL/empty) and serialized by
    ``_sweep_lock`` so the periodic scheduler and the on-read trigger never run
    overlapping sweeps. Honors the same digest settings as the 6 UTC run
    (mode/deep_research_enabled/concurrency/allow_cloud_fallback).
    """
    if _sweep_lock.locked():
        return {"status": "already_running", "attempted": 0}

    async with _sweep_lock:
        from core.api.db import acquire_db
        from core.api.services.inbox_digest import get_current_digest_cycle_key
        from core.api.services.inbox_digest_jobs import _load_digest_settings

        async with acquire_db() as db:
            settings = await _load_digest_settings(db)
            if settings.mode == "false" or not settings.deep_research_enabled:
                return {"status": "disabled", "attempted": 0}
            cycle_key = await get_current_digest_cycle_key(db, workspace_id)

            where = ["s.workspace_id = ?", "s.state = 'visible'"]
            params: list[Any] = [workspace_id]
            if cycle_key:
                where.append("s.digest_cycle_key = ?")
                params.append(cycle_key)
            params.append(limit)
            rows = await (
                await db.execute(
                    "SELECT s.inbox_item_id FROM inbox_digest_selections s "
                    "JOIN inbox_items i ON i.id = s.inbox_item_id "
                    f"WHERE {' AND '.join(where)} "
                    "AND (i.deep_research IS NULL OR i.deep_research = '') "
                    "ORDER BY s.rank_in_domain ASC LIMIT ?",
                    tuple(params),
                )
            ).fetchall()

        missing_ids = [row[0] for row in rows]
        if not missing_ids:
            return {"status": "ok", "attempted": 0, "missing": 0}

        stats = await precompute_deep_research_for_items(
            inbox_item_ids=missing_ids,
            workspace_id=workspace_id,
            cycle_key=cycle_key or "sweep",
            concurrency=settings.concurrency,
            allow_cloud_fallback=settings.allow_cloud_fallback,
        )
        stats["status"] = "ok"
        stats["missing"] = len(missing_ids)
        logger.info(
            "Digest deep-research sweep: %d visible missing -> generated=%d cached=%d failed=%d",
            len(missing_ids),
            stats.get("generated", 0),
            stats.get("cached", 0),
            stats.get("failed", 0),
        )
        return stats
