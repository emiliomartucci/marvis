# v1.1.0 - 2026-03-13 - Batch commits (50x fewer write locks per cycle)
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp

from core.api.config import settings

logger = logging.getLogger(__name__)

_http_session: aiohttp.ClientSession | None = None

# SSRF protection: reject private/reserved IPs
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
]


def _validate_webhook_url(url: str) -> bool:
    """Reject URLs pointing to private/reserved IPs. Enforce HTTPS."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    try:
        ip = socket.gethostbyname(parsed.hostname)
        addr = ipaddress.ip_address(ip)
        return not any(addr in net for net in _BLOCKED_NETWORKS)
    except Exception:
        return False


async def init_session() -> None:
    """Create reusable HTTP session. Call in lifespan startup."""
    global _http_session
    if not settings.n8n_webhook_url:
        return
    if not _validate_webhook_url(settings.n8n_webhook_url):
        logger.error("[n8n_dispatch] Webhook URL failed SSRF validation: %s", settings.n8n_webhook_url)
        return
    _http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Secret": settings.n8n_webhook_secret,
        },
    )


async def close_session() -> None:
    """Close HTTP session. Call in lifespan shutdown."""
    global _http_session
    if _http_session:
        await _http_session.close()
        _http_session = None


async def dispatch_pending_events() -> int:
    """Drain loop: read pending events and POST to n8n webhook. Returns dispatched count.

    Three-phase pattern to avoid holding the write lock during HTTP calls:
    1. READ pending events from pool (fast)
    2. HTTP dispatch outside any DB scope
    3. WRITE results via write_db (fast batch)
    """
    if not settings.n8n_webhook_url or not _http_session:
        return 0

    from core.api.db import acquire_db, write_db

    total_dispatched = 0
    max_iterations = 10

    for _ in range(max_iterations):
        # Phase 1: READ pending events from pool
        async with acquire_db() as db:
            cursor = await db.execute(
                """SELECT id, event_type, project, actor_id, target_type, target_id,
                          created_at, retry_count
                   FROM events
                   WHERE dispatched_at IS NULL
                     AND COALESCE(retry_count, 0) < ?
                   ORDER BY created_at ASC
                   LIMIT 50""",
                (settings.n8n_max_retry_count,),
            )
            rows = [dict(r) for r in await cursor.fetchall()]

        if not rows:
            break

        # Phase 2: HTTP dispatch OUTSIDE DB scope
        succeeded: list[str] = []
        failed: list[tuple[str, int]] = []
        consecutive_failures = 0

        for row in rows:
            retry_count = row["retry_count"] or 0
            try:
                payload = {
                    "event_id": row["id"],
                    "event_type": row["event_type"],
                    "project": row["project"],
                    "actor_id": row["actor_id"],
                    "target_type": row["target_type"],
                    "target_id": row["target_id"],
                    "created_at": row["created_at"],
                }
                async with _http_session.post(
                    settings.n8n_webhook_url, json=payload,
                ) as resp:
                    if resp.status < 300:
                        succeeded.append(row["id"])
                        consecutive_failures = 0
                    else:
                        body = await resp.text()
                        failed.append((row["id"], retry_count + 1))
                        consecutive_failures += 1
                        logger.warning(
                            "[n8n_dispatch] HTTP %d for event %s (retry %d/%d): %s",
                            resp.status, row["id"], retry_count + 1,
                            settings.n8n_max_retry_count, body[:200],
                        )
            except Exception:
                failed.append((row["id"], retry_count + 1))
                consecutive_failures += 1
                logger.exception("[n8n_dispatch] Failed event %s (retry %d)", row["id"], retry_count + 1)

            if consecutive_failures >= 3:
                logger.warning("[n8n_dispatch] 3 consecutive failures, aborting cycle")
                break

        # Phase 3: WRITE results via write_db (fast batch)
        if succeeded or failed:
            now_iso = datetime.now(timezone.utc).isoformat()
            async with write_db() as db:
                for event_id in succeeded:
                    await db.execute(
                        "UPDATE events SET dispatched_at = ? WHERE id = ? AND dispatched_at IS NULL",
                        (now_iso, event_id),
                    )
                for event_id, new_retry in failed:
                    await db.execute(
                        "UPDATE events SET retry_count = ? WHERE id = ?",
                        (new_retry, event_id),
                    )

        total_dispatched += len(succeeded)

        if not succeeded or consecutive_failures >= 3:
            break

    if total_dispatched > 0:
        logger.info("[n8n_dispatch] Dispatched %d events", total_dispatched)

    return total_dispatched
