from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.services.terminal_metrics import TerminalMetricsCollector
from core.api.services.terminal_metrics_dump import (
    append_terminal_client_events,
    append_terminal_metrics_record,
    run_internet_probe,
)

router = APIRouter(prefix="/api/v1/terminal", tags=["terminal"])


def get_terminal_metrics(
    request: Request, workspace_id: str | None = "ws_default"
) -> TerminalMetricsCollector:
    """Return a collector isolated to one authenticated workspace."""
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=403, detail="workspace context required")
    if workspace_id == "ws_default":
        collector = getattr(request.app.state, "terminal_metrics", None)
        if not isinstance(collector, TerminalMetricsCollector):
            collector = TerminalMetricsCollector()
            request.app.state.terminal_metrics = collector
        return collector
    collectors = getattr(request.app.state, "terminal_metrics_by_workspace", None)
    if not isinstance(collectors, dict):
        collectors = {}
        request.app.state.terminal_metrics_by_workspace = collectors
    collector = collectors.get(workspace_id)
    if not isinstance(collector, TerminalMetricsCollector):
        collector = TerminalMetricsCollector()
        collectors[workspace_id] = collector
    return collector


class TerminalMetricsBatch(BaseModel):
    run_id: str | None = None
    source: str = "console"
    exported_at: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    counters: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    browser: dict[str, Any] = Field(default_factory=dict)
    telemetry_health: dict[str, Any] = Field(default_factory=dict)


@router.get("/metrics")
async def terminal_metrics_snapshot(
    request: Request,
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
):
    collector = get_terminal_metrics(request, user.workspace_id)
    return collector.snapshot()


@router.post("/metrics-batch", status_code=204)
async def terminal_metrics_batch(
    payload: TerminalMetricsBatch,
    request: Request,
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
):
    collector = get_terminal_metrics(request, user.workspace_id)
    event_count = len(payload.events)
    counter_count = len(payload.counters)
    if event_count == 0 and counter_count == 0:
        raise HTTPException(status_code=400, detail="empty metrics batch")

    collector.record_client_event_batch(event_count)
    await append_terminal_client_events(
        {
            "run_id": payload.run_id,
            "workspace_id": user.workspace_id,
            "source": payload.source,
            "exported_at": payload.exported_at,
            "event_count": event_count,
            "counter_count": counter_count,
            "client_host": request.client.host if request.client else None,
            "browser": payload.browser,
            "telemetry_health": payload.telemetry_health,
            "events": payload.events,
            "counters": payload.counters,
        }
    )


@router.get("/network-probe")
async def terminal_network_probe(
    request: Request,
    bytes_: int = Query(65_536, alias="bytes", ge=0, le=262_144),
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
):
    collector = get_terminal_metrics(request, user.workspace_id)
    probe = await run_internet_probe()
    collector.record_internet_probe(
        target=probe["target"],
        ok=bool(probe["ok"]),
        duration_ms=float(probe["duration_ms"]),
        status_code=probe["status_code"],
        bytes_received=int(probe["bytes_received"]),
        error=probe["error"],
    )
    response = {
        "client_host": request.client.host if request.client else None,
        "server_internet_probe": probe,
        "payload_bytes": bytes_,
        "padding": "x" * bytes_,
    }
    await append_terminal_metrics_record(
        {
            "kind": "network_probe",
            "workspace_id": user.workspace_id,
            "probe": probe,
            "client_host": response["client_host"],
            "payload_bytes": bytes_,
        }
    )
    return response
