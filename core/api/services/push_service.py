# v1.1.0 - 2026-03-15 - Fix aiohttp_session param (pywebpush 2.3.0) + use acquire_db() pool
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import aiohttp
import aiosqlite
from pywebpush import webpush_async, WebPushException

from core.api.config import settings
from core.api.db import acquire_db

logger = logging.getLogger(__name__)

# Module-level session (like n8n_client.py pattern)
_push_session: aiohttp.ClientSession | None = None

ALLOWED_PUSH_ORIGINS = {
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
}
MAX_SUBSCRIPTIONS_PER_USER = 10


async def init_push_session() -> None:
    global _push_session
    _push_session = aiohttp.ClientSession()


async def close_push_session() -> None:
    global _push_session
    if _push_session:
        await _push_session.close()
        _push_session = None


def validate_push_endpoint(endpoint: str) -> bool:
    """SSRF prevention: only allow known push service origins with dot-prefix check."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    hostname = parsed.hostname
    return any(
        hostname == origin or hostname.endswith("." + origin)
        for origin in ALLOWED_PUSH_ORIGINS
    )


async def deliver_push_for_user(user_id: str, payload: dict[str, str], db: aiosqlite.Connection) -> None:
    """Send push to all subscriptions for a user. Uses caller-provided DB connection."""
    cursor = await db.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
        [user_id],
    )
    subs = await cursor.fetchall()

    vapid_claims = {"sub": "mailto:admin@justaskmarvis.com"}
    to_delete: list[str] = []

    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            await webpush_async(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims=vapid_claims.copy(),  # pywebpush mutates in place
                ttl=86400,
                aiohttp_session=_push_session,
            )
        except WebPushException as e:
            if e.response and e.response.status in (404, 410):
                to_delete.append(sub["endpoint"])
            else:
                logger.warning("Push failed for %s: %s", sub["endpoint"][:50], type(e).__name__)
        except Exception:
            logger.exception("Unexpected push error")

    if to_delete:
        await db.executemany(
            "DELETE FROM push_subscriptions WHERE endpoint = ?",
            [(ep,) for ep in to_delete],
        )
        await db.commit()
        logger.info("Cleaned %d dead push subscriptions", len(to_delete))


async def periodic_push_delivery() -> None:
    """Outbox drain: read unpushed notifications, send push, mark pushed_at.

    Three-phase pattern to avoid blocking pool=1 during HTTP push calls.
    """
    from core.api.db import write_db

    # Phase 1: READ notifications from pool (fast, release immediately)
    async with acquire_db() as db:
        cursor = await db.execute("""
            SELECT id, user_id, title, body, type
            FROM notifications
            WHERE pushed_at IS NULL AND created_at > datetime('now', '-1 hour', 'utc')
            ORDER BY created_at
            LIMIT 50
        """)
        notifications = [dict(r) for r in await cursor.fetchall()]

    if not notifications:
        return

    # Phase 2: HTTP push OUTSIDE DB scope
    pushed_ids: list[str] = []
    dead_endpoints: list[str] = []

    for notif in notifications:
        payload = {
            "title": notif["title"],
            "body": notif["body"] or "",
            "type": notif["type"],
        }
        # Read subscriptions from pool (fast)
        async with acquire_db() as db:
            cursor = await db.execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
                [notif["user_id"]],
            )
            subs = [dict(s) for s in await cursor.fetchall()]

        vapid_claims = {"sub": "mailto:admin@justaskmarvis.com"}
        for sub in subs:
            subscription_info = {
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            }
            try:
                await webpush_async(
                    subscription_info=subscription_info,
                    data=json.dumps(payload),
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims=vapid_claims.copy(),
                    ttl=86400,
                    aiohttp_session=_push_session,
                )
            except WebPushException as e:
                if e.response and e.response.status in (404, 410):
                    dead_endpoints.append(sub["endpoint"])
                else:
                    logger.warning("Push failed for %s: %s", sub["endpoint"][:50], type(e).__name__)
            except Exception:
                logger.exception("Unexpected push error")

        pushed_ids.append(notif["id"])

    # Phase 3: WRITE results via write_db (fast batch)
    async with write_db() as db:
        for nid in pushed_ids:
            await db.execute(
                "UPDATE notifications SET pushed_at = datetime('now', 'utc') WHERE id = ?",
                [nid],
            )
        if dead_endpoints:
            await db.executemany(
                "DELETE FROM push_subscriptions WHERE endpoint = ?",
                [(ep,) for ep in dead_endpoints],
            )
            logger.info("Cleaned %d dead push subscriptions", len(dead_endpoints))
