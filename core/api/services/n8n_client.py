# v1.0.0 - 2026-03-10 - n8n REST API client (session reuse)
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from core.api.config import settings

logger = logging.getLogger(__name__)

_api_session: aiohttp.ClientSession | None = None


async def init_api_session() -> None:
    global _api_session
    if settings.n8n_api_url and settings.n8n_api_key:
        _api_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"X-N8N-API-KEY": settings.n8n_api_key},
        )


async def close_api_session() -> None:
    global _api_session
    if _api_session:
        await _api_session.close()
        _api_session = None


async def list_workflows() -> list[dict[str, Any]]:
    if not _api_session:
        return []
    async with _api_session.get(f"{settings.n8n_api_url}/workflows") as resp:
        if resp.status != 200:
            logger.warning("[n8n_client] list_workflows HTTP %d", resp.status)
            return []
        data = await resp.json()
        return data.get("data", [])


async def trigger_workflow(workflow_id: str, data: dict | None = None) -> dict[str, Any]:
    """Execute a workflow with optional input data."""
    if not _api_session:
        raise ValueError("n8n API not configured")
    body = data if data else None
    async with _api_session.post(
        f"{settings.n8n_api_url}/workflows/{workflow_id}/execute",
        json=body,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise ValueError(f"n8n execute failed HTTP {resp.status}: {text[:200]}")
        return await resp.json()


async def get_executions(
    workflow_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    if not _api_session:
        return []
    params: dict[str, Any] = {"limit": limit}
    if workflow_id:
        params["workflowId"] = workflow_id
    if status:
        params["status"] = status
    async with _api_session.get(
        f"{settings.n8n_api_url}/executions", params=params
    ) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
        return data.get("data", [])
