from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from core.api.services.inbox_tldr import get_or_generate_deep_research

logger = logging.getLogger(__name__)


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
