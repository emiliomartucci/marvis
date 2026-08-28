# v1.0.0 - 2026-04-15 - KG Phase 4.5: agent control plane (kg_reindex_path/kg_rebuild/kg_watcher_control)
"""KG Phase 4.5 — agent control plane.

3 endpoint che danno agli agent (operator+) il controllo runtime del KG senza
dover invocare systemctl/CLI. Risponde a 7/7 scenari agent-native (vs 5/7
pre-Phase 4.5):

- POST /api/v1/kg/reindex_path: invoca populate_artifacts/cross_project
  --incremental su 1+ path. Sync, ritorna risultati.
- POST /api/v1/kg/rebuild: enqueue full-rebuild (background). Allinea shape
  con mcp__marvis__reindex (semantic, non graph) per consistenza.
- POST /api/v1/kg/watcher_control: pause/resume del kg-watcher daemon via
  sentinel file. Auto-resume dopo `duration_seconds`.

## Design

I 3 endpoint NON modificano il DB direttamente. Delegano a:
- subprocess `python -m core.scripts.populate_*` (sync) per reindex_path
- `systemctl --user start pir-kg-full-rebuild.service` (background) per rebuild
- touch/rm sentinel file `/run/user/$UID/pir-kg-watcher/paused` per
  watcher_control (il daemon legge il file in dispatch_loop)

Pattern coerente con altri route che orchestrano subprocess (finder),
zero nesting lock single-writer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.api.db import get_write_db
from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.security import is_local_single_user_mode, is_loopback_request
from core.api.services.kg import manual_edges
from core.api.services.kg_watcher_control import (
    WatcherSentinelError,
    is_paused,
    pause_watcher,
    resume_watcher,
)

logger = logging.getLogger("api.routers.kg")

router = APIRouter(prefix="/api/v1/kg", tags=["kg"])

_LOCAL_HOST_DETAIL = (
    "This host-global KG control operation is available only to the trusted "
    "local OSS loopback runtime."
)


def _require_local_host_request(request: Request) -> None:
    if is_local_single_user_mode() and is_loopback_request(request):
        return
    raise HTTPException(status_code=403, detail=_LOCAL_HOST_DETAIL)


# Resolve paths matching deploy layout: scripts/ at /data/pir/scripts/.
SCRIPTS_ROOT = Path("/data/pir")
PYTHON_BIN = Path(os.environ.get("KG_PYTHON_BIN", "/data/pir/venv/bin/python"))

SUBPROCESS_TIMEOUT = 60  # populate_*_incremental tipicamente <500ms-1s


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class ReindexPathRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)
    mode: Literal["artifact", "cross_project", "both"] = "both"
    handle_delete: bool = False
    skip_hash_gate: bool = False


class ReindexPathResponse(BaseModel):
    nodes_written: int = 0
    edges_written: int = 0
    files_processed: int = 0
    files_skipped_hash_unchanged: int = 0
    skipped: list[dict[str, Any]] = []
    latency_ms: float = 0.0


class RebuildRequest(BaseModel):
    scope: Literal["all", "default"] = "all"


class RebuildResponse(BaseModel):
    status: Literal["queued", "already_running"]
    job_id: str


class WatcherControlRequest(BaseModel):
    action: Literal["pause", "resume", "status"]
    duration_seconds: int | None = Field(default=None, ge=10, le=86400)


class WatcherControlResponse(BaseModel):
    state: Literal["active", "paused", "inactive", "failed", "unknown"]
    paused_until: str | None = None


class ManualProjectEdgeRequest(BaseModel):
    src_slug: str = Field(..., min_length=1, max_length=63)
    dst_slug: str = Field(..., min_length=1, max_length=63)
    kind: Literal["related", "depends_on"]


class ManualProjectEdgeOut(BaseModel):
    src_slug: str
    dst_slug: str
    kind: Literal["related", "depends_on"]
    provenance: Literal["manual"] = "manual"


class ManualProjectEdgeUpsertResponse(BaseModel):
    created: bool
    edge: ManualProjectEdgeOut


class ManualProjectEdgeDeleteResponse(BaseModel):
    deleted: bool
    edge: ManualProjectEdgeOut


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_populator(
    module: str,
    paths: list[str],
    *,
    handle_delete: bool,
    skip_hash_gate: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Invoke `python -m <module> --incremental <paths>` con timeout."""
    cmd: list[str] = [str(PYTHON_BIN), "-m", module]
    if handle_delete:
        cmd.append("--handle-delete")
    if skip_hash_gate:
        cmd.append("--skip-hash-gate")
    cmd.append("--incremental")
    cmd.extend(paths)

    env = {**os.environ, "KG_HOOK_DISABLED": "1"}  # avoid recursive trigger
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(SCRIPTS_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        logger.error("kg subprocess spawn failed (%s): %s", module, e)
        return None, []

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.error("kg subprocess %s timed out (%ds) — killing", module, SUBPROCESS_TIMEOUT)
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        return None, []

    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

    if proc.returncode != 0:
        logger.error("kg subprocess %s exit=%s stderr=%s", module, proc.returncode, stderr[:500])
        return None, []

    result: dict[str, Any] | None = None
    try:
        result = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        logger.warning("kg subprocess %s stdout not JSON: %s", module, stdout[:200])

    skipped: list[dict[str, Any]] = []
    if stderr.strip():
        try:
            stderr_obj = json.loads(stderr)
            if isinstance(stderr_obj, dict) and isinstance(stderr_obj.get("skipped"), list):
                skipped = stderr_obj["skipped"]
        except json.JSONDecodeError:
            pass

    return result, skipped


async def _systemctl_user(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Wrapper async per `systemctl --user <args>`. Returns (rc, stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout_b.decode("utf-8", errors="replace").strip()
    except (asyncio.TimeoutError, OSError):
        return 1, ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _manual_edge_out(edge: manual_edges.ManualProjectEdge) -> ManualProjectEdgeOut:
    return ManualProjectEdgeOut(
        src_slug=edge.src_slug,
        dst_slug=edge.dst_slug,
        kind=edge.kind,
        provenance="manual",
    )


@router.post(
    "/edges/manual",
    status_code=201,
    response_model=ManualProjectEdgeUpsertResponse,
)
async def upsert_manual_project_edge(
    req: ManualProjectEdgeRequest,
    user: UserInfo = Depends(require_role("operator")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> ManualProjectEdgeUpsertResponse:
    try:
        edge, created = await manual_edges.upsert_manual_project_edge(
            db,
            workspace_id=user.workspace_id or "ws_default",
            src_slug=req.src_slug,
            dst_slug=req.dst_slug,
            kind=req.kind,
            created_by=user.user_id or user.username,
        )
    except manual_edges.ManualEdgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return ManualProjectEdgeUpsertResponse(created=created, edge=_manual_edge_out(edge))


@router.delete(
    "/edges/manual",
    response_model=ManualProjectEdgeDeleteResponse,
)
async def delete_manual_project_edge(
    req: ManualProjectEdgeRequest,
    user: UserInfo = Depends(require_role("operator")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> ManualProjectEdgeDeleteResponse:
    try:
        edge, deleted = await manual_edges.delete_manual_project_edge(
            db,
            workspace_id=user.workspace_id or "ws_default",
            src_slug=req.src_slug,
            dst_slug=req.dst_slug,
            kind=req.kind,
        )
    except manual_edges.ManualEdgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return ManualProjectEdgeDeleteResponse(deleted=deleted, edge=_manual_edge_out(edge))


@router.post(
    "/reindex_path",
    response_model=ReindexPathResponse,
    dependencies=[Depends(_require_local_host_request)],
)
async def reindex_path(
    req: ReindexPathRequest,
    _=Depends(require_role("operator")),
) -> ReindexPathResponse:
    """Re-indicizza N path su artifacts e/o cross_project. Sync (timeout 60s)."""
    t0 = time.perf_counter()
    files_processed = 0
    nodes_written = 0
    edges_written = 0
    files_skipped = 0
    skipped: list[dict[str, Any]] = []

    if req.mode in ("artifact", "both"):
        out, sk = await _run_populator(
            "core.scripts.populate_artifacts",
            req.paths,
            handle_delete=req.handle_delete,
            skip_hash_gate=req.skip_hash_gate,
        )
        if out:
            files_processed = max(files_processed, int(out.get("files_processed", 0)))
            nodes_written += int(out.get("nodes_written", 0))
            edges_written += int(out.get("edges_written", 0))
            files_skipped += int(out.get("files_skipped_hash_unchanged", 0))
        skipped.extend(sk)

    if req.mode in ("cross_project", "both"):
        out, sk = await _run_populator(
            "core.scripts.populate_cross_project",
            req.paths,
            handle_delete=req.handle_delete,
            skip_hash_gate=req.skip_hash_gate,
        )
        if out:
            files_processed = max(files_processed, int(out.get("files_processed", 0)))
            nodes_written += int(out.get("nodes_written", 0))
            edges_written += int(out.get("edges_written", 0))
            files_skipped += int(out.get("files_skipped_hash_unchanged", 0))
        skipped.extend(sk)

    return ReindexPathResponse(
        nodes_written=nodes_written,
        edges_written=edges_written,
        files_processed=files_processed,
        files_skipped_hash_unchanged=files_skipped,
        skipped=skipped,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
    )


@router.post(
    "/rebuild",
    response_model=RebuildResponse,
    dependencies=[Depends(_require_local_host_request)],
)
async def trigger_rebuild(
    req: RebuildRequest,
    _=Depends(require_role("operator")),
) -> RebuildResponse:
    """Trigger pir-kg-full-rebuild.service (oneshot). Background.

    Lo script bash gestisce internamente: backup-db.sh + stop watcher +
    populate_* + start watcher (trap EXIT). `scope=all` e' default — il
    rebuild gia' usa --include-all-projects (Phase 3). `scope=default`
    riservato per future opzioni runtime (oggi no-op identico).
    """
    _ = req.scope  # reserved for future runtime override

    rc, status_text = await _systemctl_user(
        ["is-active", "pir-kg-full-rebuild.service"]
    )
    if status_text in ("active", "activating"):
        return RebuildResponse(status="already_running", job_id="-")

    rc, _ = await _systemctl_user(
        ["start", "--no-block", "pir-kg-full-rebuild.service"]
    )
    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail=f"systemctl start pir-kg-full-rebuild.service failed (rc={rc})",
        )
    return RebuildResponse(status="queued", job_id=str(uuid.uuid4()))


@router.post(
    "/watcher_control",
    response_model=WatcherControlResponse,
    dependencies=[Depends(_require_local_host_request)],
)
async def watcher_control(
    req: WatcherControlRequest,
    _=Depends(require_role("operator")),
) -> WatcherControlResponse:
    """Pause/resume/status del kg-watcher daemon via sentinel file.

    Pause: touch /run/user/$UID/pir-kg-watcher/paused. Il daemon vede il file
    in dispatch_loop e drena la queue senza dispatch (no scritture su DB).
    Auto-resume opzionale dopo `duration_seconds` via asyncio.create_task.

    Resume: rm sentinel file. Il daemon riprende al prossimo tick (~1s).

    Status: legge sentinel + systemctl. Returns watcher state.
    """
    if req.action == "status":
        return await _watcher_status_response()

    if req.action == "resume":
        resume_watcher()
        return await _watcher_status_response()

    if req.action == "pause":
        try:
            paused_until = pause_watcher(req.duration_seconds)
        except WatcherSentinelError as exc:
            raise HTTPException(
                status_code=500,
                detail=str(exc),
            ) from exc

        if req.duration_seconds:
            asyncio.create_task(_auto_resume(req.duration_seconds))

        resp = await _watcher_status_response()
        resp.paused_until = paused_until
        return resp

    raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")


async def _watcher_status_response() -> WatcherControlResponse:
    _, text = await _systemctl_user(["is-active", "pir-kg-watcher.service"])
    if is_paused():
        return WatcherControlResponse(state="paused")
    if text == "active":
        return WatcherControlResponse(state="active")
    if text == "inactive":
        return WatcherControlResponse(state="inactive")
    if text == "failed":
        return WatcherControlResponse(state="failed")
    return WatcherControlResponse(state="unknown")


async def _auto_resume(delay_seconds: int) -> None:
    """Background task: dopo `delay_seconds` rimuove la sentinel se ancora presente."""
    try:
        await asyncio.sleep(delay_seconds)
        resume_watcher()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — background task must not crash the loop
        logger.warning("kg watcher auto-resume failed: %s", e)
