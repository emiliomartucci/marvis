"""Voyage retry helper with filesystem fallback for durable replay."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DELAYS_MS: list[int] = [0, 400, 1500, 4000]
KV_FALLBACK_DIR = Path(
    os.environ.get("INGEST_VOYAGE_FAILED_DIR", "/data/pir/ingest_voyage_failed")
)
VOYAGE_EMBEDDINGS_URL = "https://api.voyageai.com/v1/embeddings"


def _voyage_headers() -> dict[str, str]:
    key = os.environ.get("VOYAGE_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _write_fallback_file(name: str, payload: dict[str, Any]) -> Path:
    KV_FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    path = KV_FALLBACK_DIR / name
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    return path


async def retry_voyage_with_fallback(ingest_id: str, payload: dict[str, Any]) -> bool:
    """Retry Voyage embeddings and persist failed payloads for later replay.

    Returns True when Voyage accepts the request during the retry window.
    Returns False when the request is durably written to the fallback KV dir.
    """
    last_status: int | None = None
    last_error: str | None = None

    for attempt, delay_ms in enumerate(DELAYS_MS, start=1):
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    VOYAGE_EMBEDDINGS_URL,
                    json=payload,
                    headers=_voyage_headers(),
                )
        except (httpx.RequestError, asyncio.TimeoutError) as exc:
            last_error = str(exc)
            logger.warning("voyage request error attempt=%d ingest=%s: %s", attempt, ingest_id, exc)
            continue

        last_status = response.status_code
        if 200 <= response.status_code < 300:
            return True
        if 400 <= response.status_code < 500 and response.status_code != 429:
            last_error = response.text[:500]
            logger.error("voyage permanent failure status=%s ingest=%s", response.status_code, ingest_id)
            break
        last_error = response.text[:500]
        logger.warning("voyage transient failure status=%s attempt=%d ingest=%s", response.status_code, attempt, ingest_id)

    _write_fallback_file(
        f"{ingest_id}.json",
        {
            "ingest_id": ingest_id,
            "payload": payload,
            "attempts": len(DELAYS_MS),
            "last_status": last_status,
            "last_error": last_error,
        },
    )
    return False


async def enqueue_voyage_removal(ingest_id: str) -> None:
    """Persist a low-priority removal request for a later Voyage cleanup worker."""
    _write_fallback_file(
        f"REMOVE_{ingest_id}.json",
        {"action": "remove", "ingest_id": ingest_id},
    )
