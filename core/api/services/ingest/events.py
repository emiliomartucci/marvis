"""Best-effort websocket notifications for ingest state changes."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def broadcast_ingest_changed(
    event: str,
    *,
    ingest_id: str | None = None,
    project_slug: str | None = None,
    status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"type": "ingest_changed", "event": event}
    if ingest_id is not None:
        payload["ingest_id"] = ingest_id
    if project_slug is not None:
        payload["project_slug"] = project_slug
    if status is not None:
        payload["status"] = status
    if extra:
        payload.update(extra)

    try:
        from core.api.terminal import session_manager

        await session_manager.broadcast_control_message(payload)
    except Exception:
        logger.exception("failed to broadcast ingest_changed event")
