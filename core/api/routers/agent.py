# v1.5.0 - 2026-04-14 - Single-writer: send_input uses get_write_db (batch 6/6)
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from time import monotonic

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi import Path as PathParam

from core.api.config import settings
from core.api.db import acquire_db, get_db, get_write_db, write_db
from core.api.models import (
    AgentSessionUpdate,
    AgentSessionView,
    ExecBody,
    InputBody,
    PaneResponse,
    UserInfo,
)
from core.api.security import get_agent_user
from core.api.services import claude_metrics, opencode_sessions, tmux
from core.api.services.access_grants import (
    actor_from_user_info,
    require_unique_project_for_actor,
)
from core.api.services.project_paths import candidate_project_paths, resolve_project_path
from core.api.services.providers import build_start_command, get_provider
from core.api.services.session_ops import (
    build_session_start_spec,
    get_live_session_data,
    get_session_row_by_uuid,
    hibernate_session_core,
)
from core.api.services.session_operation_leases import (
    SessionOperationBusy,
    SessionOperationMissing,
    acquire_session_operation_lease,
    release_session_operation_lease,
    session_operation_lease,
)
from core.api.use_cases._errors import NotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

# UUID v4 pattern — version nibble = 4, variant bits = [89ab]
_UUID_V4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Conversation ID pattern (UUID v4 or v1 from Claude JSONL)
_CONVERSATION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# --- Rate limiter (in-memory sliding window, 30 req/min per tenant + agent) ---
# IMPORTANT: Single-process only. Not shared across uvicorn workers.
# Safe for single-worker deployment (current production setup).
# /exec uses cost=2 (counts double against the limit).
_RATE_WINDOW = 60.0
_RATE_LIMIT = 30
_rate_store: dict[tuple[str, str], list[float]] = defaultdict(list)


def _rate_check(workspace_id: str, agent_name: str, cost: int = 1) -> None:
    """Raise 429 if the agent exceeds 30 tokens/min."""
    bucket_key = (workspace_id, agent_name)
    now = monotonic()
    filtered = [t for t in _rate_store[bucket_key] if now - t < _RATE_WINDOW]
    if filtered:
        _rate_store[bucket_key] = filtered
    else:
        _rate_store.pop(bucket_key, None)  # cleanup stale keys
    if len(_rate_store.get(bucket_key, [])) + cost > _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (30 req/min)")
    bucket = _rate_store[bucket_key]
    for _ in range(cost):
        bucket.append(now)


# --- Circuit breaker (DB-backed, per-session hourly limit for DevX auto-responses) ---
_CIRCUIT_BREAKER_LIMIT = 10
_CIRCUIT_BREAKER_WINDOW = timedelta(hours=1)


async def _circuit_breaker_check(
    agent_name: str,
    session_uuid: str,
    db: aiosqlite.Connection,
) -> None:
    """Raise 429 if DevX exceeds 10 exec actions per session in the last hour.

    Only applies to agent_name == "devx". Other agents are not affected.
    Queries the agent_actions audit table for recent exec actions.
    """
    if agent_name != "devx":
        return

    cutoff = (datetime.utcnow() - _CIRCUIT_BREAKER_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    cursor = await db.execute(
        "SELECT COUNT(*) FROM agent_actions"
        " WHERE session_uuid = ? AND agent_name = ? AND action = 'exec' AND created_at > ?",
        (session_uuid, agent_name, cutoff),
    )
    row = await cursor.fetchone()
    count = row[0] if row else 0

    if count >= _CIRCUIT_BREAKER_LIMIT:
        logger.warning(
            "Circuit breaker tripped: agent=%s session=%s count=%d limit=%d",
            agent_name,
            session_uuid,
            count,
            _CIRCUIT_BREAKER_LIMIT,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Circuit breaker: {count} exec actions in past hour for session {session_uuid}",
        )


# --- Helper: resolve UUID -> tmux name ---
def _workspace_for_agent(user: UserInfo) -> str:
    workspace_id = (user.workspace_id or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=403, detail="Session not found")
    return workspace_id


def _agent_request_scope(user: UserInfo, *, cost: int = 1) -> tuple[str, str]:
    """Return authenticated agent/workspace and charge its isolated rate bucket."""
    workspace_id = _workspace_for_agent(user)
    agent_name = user.username.removeprefix("agent:")
    _rate_check(workspace_id, agent_name, cost=cost)
    return agent_name, workspace_id


async def _require_agent_project_path(
    db: aiosqlite.Connection,
    user: UserInfo,
    project_slug: str | None,
) -> None:
    """Fail closed before a remote session can resolve a shared project path."""
    project = (project_slug or "").strip()
    if not project:
        return
    try:
        await require_unique_project_for_actor(
            db,
            actor_from_user_info(user),
            project,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _agent_session_operation(operation: str):
    """Serialize one agent lifecycle endpoint across processes and tmux I/O."""

    def decorator(func):
        @wraps(func)
        async def wrapped(*args, **kwargs):
            user: UserInfo = kwargs["user"]
            session_uuid: str = kwargs["uuid"]
            workspace_id = _workspace_for_agent(user)
            route_db = kwargs.get("db")

            if route_db is None:
                async with acquire_db() as read_db:
                    name = await _resolve_uuid(
                        session_uuid,
                        read_db,
                        workspace_id,
                    )
                try:
                    async with session_operation_lease(
                        workspace_id=workspace_id,
                        session_name=name,
                        session_uuid=session_uuid,
                        operation=operation,
                    ):
                        return await func(*args, **kwargs)
                except SessionOperationMissing as exc:
                    raise HTTPException(404, "Session not found") from exc
                except SessionOperationBusy as exc:
                    raise HTTPException(
                        409,
                        "Session lifecycle operation already in progress",
                    ) from exc

            name = await _resolve_uuid(session_uuid, route_db, workspace_id)
            try:
                lease = await acquire_session_operation_lease(
                    route_db,
                    workspace_id=workspace_id,
                    session_name=name,
                    session_uuid=session_uuid,
                    operation=operation,
                )
                await route_db.commit()
            except SessionOperationMissing as exc:
                raise HTTPException(404, "Session not found") from exc
            except SessionOperationBusy as exc:
                raise HTTPException(
                    409,
                    "Session lifecycle operation already in progress",
                ) from exc

            try:
                result = await func(*args, **kwargs)
            except BaseException:
                await route_db.rollback()
                await release_session_operation_lease(route_db, lease)
                await route_db.commit()
                raise
            await release_session_operation_lease(route_db, lease)
            await route_db.commit()
            return result

        return wrapped

    return decorator


async def _resolve_uuid(
    session_uuid: str,
    db: aiosqlite.Connection,
    workspace_id: str,
) -> str:
    """Convert session_uuid to tmux name. Raises 404 if not found."""
    cursor = await db.execute(
        "SELECT name FROM sessions_meta "
        "WHERE session_uuid = ? AND workspace_id = ?",
        (session_uuid, workspace_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return row["name"]


# --- Audit log (fire-and-forget, non-critical) ---
async def _do_log(
    db_path: str,
    agent_name: str,
    action: str,
    session_uuid: str | None,
    session_name: str | None,
    payload: str | None,
    result: str,
    detail: str | None,
) -> None:
    try:
        from core.api.db import write_db
        async with write_db() as db:
            await db.execute(
                """INSERT INTO agent_actions
                   (agent_name, session_uuid, session_name, action, payload, result, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent_name,
                    session_uuid,
                    session_name,
                    action,
                    payload,
                    result,
                    detail,
                ),
            )
    except Exception as e:  # Non-critical: never block agent action on audit failure
        logger.warning("Failed to write agent_actions log: %s", e)


def _log_action(
    agent_name: str,
    action: str,
    session_uuid: str | None = None,
    session_name: str | None = None,
    payload: str | None = None,
    result: str = "ok",
    detail: str | None = None,
) -> None:
    """Schedule audit log write as fire-and-forget background task.

    Non-critical per kb/api-patterns.md — never blocks the agent action response.
    Opens fresh DB connection to avoid holding request-scoped connection.
    """
    asyncio.create_task(
        _do_log(
            settings.db_path,
            agent_name,
            action,
            session_uuid,
            session_name,
            payload,
            result,
            detail,
        )
    )


# --- Helper: build AgentSessionView from DB row + live data ---
def _build_view(row: dict, live: dict) -> AgentSessionView:
    uptime: float | None = None
    created_at = row.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            uptime = (datetime.now(timezone.utc) - created).total_seconds()
        except (ValueError, AttributeError):
            pass

    return AgentSessionView(
        name=row["name"],
        session_uuid=row.get("session_uuid"),
        project_slug=row.get("project_slug"),
        status=live.get("status"),
        activity_state=live.get("activity_state"),
        last_context_pct=row.get("last_context_pct"),
        last_cost_usd=row.get("last_cost_usd"),
        cpu_pct=live.get("cpu_pct"),
        ram_mb=live.get("ram_mb"),
        working_seconds=row.get("working_seconds") or 0,
        uptime_seconds=uptime,
        hibernated=bool(row.get("hibernated")),
        conversation_id=row.get("conversation_id"),
        model=row.get("model"),
        launch_model=row.get("launch_model"),
        agent_managed=bool(row.get("agent_managed")),
    )


# ===========================================================================
# Endpoints
# ===========================================================================


@router.get("/sessions", response_model=list[AgentSessionView])
async def list_sessions(
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
    agent_managed: bool | None = Query(default=None),
) -> list[AgentSessionView]:
    """Lista tutte le sessioni tmux con metriche complete.

    Usa _sync_sessions per garantire coerenza tmux-DB (appropriate per list).
    Per single-session lookup usare GET /sessions/{uuid} (targeted, no full sync).
    agent_managed=true: filtra solo sessioni monitorate da DevX.
    """
    agent_name, workspace_id = _agent_request_scope(user)

    from core.api.routers.sessions import _sync_sessions

    sessions = await _sync_sessions(db, workspace_id=workspace_id)

    if agent_managed is not None:
        sessions = [s for s in sessions if s.agent_managed == agent_managed]

    _log_action(agent_name, "list")

    now = datetime.now(timezone.utc)
    result = []
    for s in sessions:
        uptime: float | None = None
        if s.created_at:
            try:
                created = datetime.fromisoformat(s.created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                uptime = (now - created).total_seconds()
            except (ValueError, AttributeError):
                pass
        result.append(
            AgentSessionView(
                name=s.name,
                session_uuid=s.session_uuid,
                project_slug=s.project_slug,
                status=s.status,
                activity_state=s.activity_state,
                last_context_pct=s.last_context_pct,
                last_cost_usd=s.last_cost_usd,
                cpu_pct=s.cpu_pct,
                ram_mb=s.ram_mb,
                working_seconds=s.working_seconds or 0,
                uptime_seconds=uptime,
                hibernated=s.hibernated,
                conversation_id=s.conversation_id,
                model=s.model,
                launch_model=s.launch_model,
                agent_managed=s.agent_managed,
            )
        )
    return result


@router.get("/sessions/{uuid}", response_model=AgentSessionView)
async def get_session(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> AgentSessionView:
    """Singola sessione per UUID. Targeted O(1) — NON chiama _sync_sessions."""
    agent_name, workspace_id = _agent_request_scope(user)

    result = await get_session_row_by_uuid(
        uuid, db, workspace_id=workspace_id
    )
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")
    name, row = result

    live = await get_live_session_data(name)
    _log_action(agent_name, "get", session_uuid=uuid, session_name=name)
    return _build_view(row, live)


@router.patch("/sessions/{uuid}", response_model=AgentSessionView)
async def update_session(
    body: AgentSessionUpdate,
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> AgentSessionView:
    """Aggiorna project_slug o display_name.

    Usa model_fields_set per distinguere 'campo non inviato' da 'campo inviato come null'.
    Inviare {"project_slug": null} rimuove il collegamento progetto.
    """
    agent_name, workspace_id = _agent_request_scope(user)

    name = await _resolve_uuid(uuid, db, workspace_id)
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    updates, params = [], []
    if "project_slug" in body.model_fields_set:
        await _require_agent_project_path(db, user, body.project_slug)
        updates.append("project_slug = ?")
        params.append(body.project_slug or None)
    if "display_name" in body.model_fields_set:
        updates.append("display_name = ?")
        params.append(body.display_name or None)
    if "agent_managed" in body.model_fields_set and body.agent_managed is not None:
        # Agents can set agent_managed only on sessions they own (by session_uuid owner check)
        # Re-fetch row to check ownership context — use get_session_row_by_uuid result
        session_result = await get_session_row_by_uuid(
            uuid, db, workspace_id=workspace_id
        )
        if session_result:
            _, session_row = session_result
            # No owner_id on sessions_meta — agents manage their own sessions via UUID
            # Any authenticated agent with valid Bearer token may toggle agent_managed
        updates.append("agent_managed = ?")
        params.append(1 if body.agent_managed else 0)

    if updates:
        params.extend((name, workspace_id))
        await db.execute(
            f"UPDATE sessions_meta SET {', '.join(updates)} "
            "WHERE name = ? AND workspace_id = ?",
            params,
        )
        await db.commit()

    payload = body.model_dump_json()
    _log_action(
        agent_name, "patch", session_uuid=uuid, session_name=name, payload=payload
    )

    # Re-fetch targeted (no _sync_sessions)
    result = await get_session_row_by_uuid(
        uuid, db, workspace_id=workspace_id
    )
    if not result:
        raise HTTPException(status_code=404, detail="Session not found after update")
    _, row = result
    live = await get_live_session_data(name)
    return _build_view(row, live)


@router.delete("/sessions/{uuid}", status_code=204)
@_agent_session_operation("delete")
async def delete_session(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Kill sessione tmux per UUID.

    Ordine operazioni: audit PRIMA del delete (garantisce trace anche su crash).
    Pulisce session_costs per evitare orphan rows con cost data corrotto.
    """
    agent_name, workspace_id = _agent_request_scope(user)

    name = await _resolve_uuid(uuid, db, workspace_id)
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Write audit BEFORE delete (crash-safe ordering)
    _log_action(agent_name, "kill", session_uuid=uuid, session_name=name)

    success = await tmux.kill_session(name)
    if not success:
        _log_action(
            agent_name,
            "kill",
            session_uuid=uuid,
            session_name=name,
            result="error",
            detail="tmux kill failed",
        )
        raise HTTPException(status_code=500, detail="Failed to kill session")

    # Null out session_name in session_costs (preserve cost history by conversation_id)
    await db.execute(
        "UPDATE session_costs SET session_name = NULL "
        "WHERE workspace_id = ? AND session_name = ?",
        (workspace_id, name),
    )
    await db.execute(
        "DELETE FROM sessions_meta WHERE name = ? AND workspace_id = ?",
        (name, workspace_id),
    )
    await db.commit()

    logger.info("Agent %s killed session %s (uuid=%s)", agent_name, name, uuid)


@router.post("/sessions/{uuid}/hibernate", status_code=202)
async def hibernate_session(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    background_tasks: BackgroundTasks = None,
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Iberna sessione. Risposta 202 immediata — sleep sequence in background task.

    Polling GET /sessions/{uuid} per rilevare quando hibernated=true.
    Usa hibernate_session_core() condiviso con sessions router.
    """
    agent_name, workspace_id = _agent_request_scope(user)

    name = await _resolve_uuid(uuid, db, workspace_id)
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    cursor = await db.execute(
        "SELECT hibernated, conversation_id, provider, project_slug, launch_model, permission_preset, bootstrap_message, created_at "
        "FROM sessions_meta WHERE name = ? AND workspace_id = ?",
        (name, workspace_id),
    )
    row = await cursor.fetchone()
    if row and row["hibernated"]:
        raise HTTPException(status_code=409, detail="Session already hibernated")

    conv_id = row["conversation_id"] if row else None
    session_provider = row["provider"] if row and row["provider"] else "claude"
    project_slug = row["project_slug"] if row else None
    await _require_agent_project_path(db, user, project_slug)

    try:
        async with write_db(label="agent.hibernate.acquire_lease") as lease_db:
            lease = await acquire_session_operation_lease(
                lease_db,
                workspace_id=workspace_id,
                session_name=name,
                session_uuid=uuid,
                operation="hibernate",
            )
    except SessionOperationMissing as exc:
        raise HTTPException(404, "Session not found") from exc
    except SessionOperationBusy as exc:
        raise HTTPException(
            409,
            "Session lifecycle operation already in progress",
        ) from exc

    # Background task opens its own DB connection to avoid holding request connection
    # through the ~4s sleep sequence in hibernate_session_core
    async def _do_hibernate_bg() -> None:
        try:
            await hibernate_session_core(
                name,
                conv_id=conv_id,
                provider=session_provider,
                project_slug=project_slug,
                workspace_id=workspace_id,
                session_uuid=uuid,
            )
        finally:
            async with write_db(label="agent.hibernate.release_lease") as lease_db:
                await release_session_operation_lease(lease_db, lease)

    if background_tasks is not None:
        background_tasks.add_task(_do_hibernate_bg)
    else:
        asyncio.create_task(_do_hibernate_bg())

    _log_action(agent_name, "hibernate", session_uuid=uuid, session_name=name)
    logger.info("Agent %s triggered hibernate for session %s", agent_name, name)
    return {"status": "hibernating", "session_uuid": uuid}


@router.post("/sessions/{uuid}/resume", status_code=200)
@_agent_session_operation("resume")
async def resume_session(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Riprende sessione ibernata con --resume <conversation_id>."""
    agent_name, workspace_id = _agent_request_scope(user)

    name = await _resolve_uuid(uuid, db, workspace_id)
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    cursor = await db.execute(
        "SELECT hibernated, conversation_id, provider, project_slug, launch_model, permission_preset, theme_mode, bootstrap_message, created_at "
        "FROM sessions_meta WHERE name = ? AND workspace_id = ?",
        (name, workspace_id),
    )
    row = await cursor.fetchone()
    if not row or not row["hibernated"]:
        raise HTTPException(status_code=409, detail="Session is not hibernated")

    session_provider = row["provider"] if row["provider"] else "claude"
    provider_config = get_provider(session_provider)
    is_claude = session_provider == "claude"
    conv_id = row["conversation_id"]
    project_slug = row["project_slug"] if row else None
    await _require_agent_project_path(db, user, project_slug)
    project_path = resolve_project_path(project_slug)
    launch_model = row["launch_model"] if row else None
    permission_preset = row["permission_preset"] if row else None

    if is_claude and conv_id and not _CONVERSATION_ID_RE.match(conv_id):
        conv_id = None

    if is_claude and conv_id:
        conv_cwd = await asyncio.to_thread(
            claude_metrics.find_conversation_cwd,
            conv_id,
            candidate_project_paths(project_slug),
        )
        if conv_cwd:
            cmd = build_start_command(
                provider_config, conv_cwd, model=launch_model
            ).replace(
                f"{provider_config.binary} {provider_config.cli_flags}",
                f"claude --resume {conv_id} --dangerously-skip-permissions",
            )
        else:
            conv_id = None
            await db.execute(
                "UPDATE sessions_meta SET conversation_id = NULL "
                "WHERE name = ? AND workspace_id = ?",
                (name, workspace_id),
            )
            cmd = build_start_command(
                provider_config, project_path, model=launch_model
            ).replace(
                f"{provider_config.binary} {provider_config.cli_flags}",
                "claude --continue --dangerously-skip-permissions",
            )
    elif is_claude:
        cmd = build_start_command(
            provider_config, project_path, model=launch_model
        ).replace(
            f"{provider_config.binary} {provider_config.cli_flags}",
            "claude --continue --dangerously-skip-permissions",
        )
    elif session_provider == "opencode":
        launch_spec = build_session_start_spec(
            session_provider,
            project_slug,
            launch_model,
            permission_preset,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        )
        if not opencode_sessions.is_opencode_session_id(conv_id):
            conv_id = await asyncio.to_thread(
                opencode_sessions.find_session_id_for_created_at,
                launch_spec.launch_dir,
                row["created_at"] if row else None,
            )
            if conv_id:
                await db.execute(
                    "UPDATE sessions_meta SET conversation_id = ? "
                    "WHERE name = ? AND workspace_id = ?",
                    (conv_id, name, workspace_id),
                )
        cmd = build_session_start_spec(
            session_provider,
            project_slug,
            launch_model,
            permission_preset,
            resume_session_id=conv_id,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        ).start_command
    else:
        cmd = build_session_start_spec(
            session_provider,
            project_slug,
            launch_model,
            permission_preset,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        ).start_command

    if session_provider == "opencode":
        launched_at_ms = int(_time.time() * 1000)
        if await tmux.session_exists(name) and not await tmux.kill_session(name):
            raise HTTPException(
                status_code=500, detail="Failed to restart tmux session"
            )
        if not await tmux.create_session(name, start_command=cmd):
            raise HTTPException(status_code=500, detail="Failed to restart session")
        if not conv_id:
            launch_spec = build_session_start_spec(
                session_provider,
                project_slug,
                launch_model,
                permission_preset,
                session_name=name,
                theme_mode=row["theme_mode"] if row else None,
            )
            conv_id = await opencode_sessions.wait_for_new_session_id(
                launch_spec.launch_dir,
                launched_at_ms,
            )
            if conv_id:
                await db.execute(
                    "UPDATE sessions_meta SET conversation_id = ? "
                    "WHERE name = ? AND workspace_id = ?",
                    (conv_id, name, workspace_id),
                )
    else:
        await tmux.send_keys(
            name, cmd, double_enter=provider_config.submit_with_double_enter
        )
    await db.execute(
        "UPDATE sessions_meta SET hibernated = 0, hibernated_at = NULL "
        "WHERE name = ? AND workspace_id = ?",
        (name, workspace_id),
    )
    await db.commit()

    _log_action(agent_name, "resume", session_uuid=uuid, session_name=name)
    logger.info(
        "Agent %s resumed session %s (conversation=%s)", agent_name, name, conv_id
    )
    return {"status": "resumed", "conversation_id": conv_id}


@router.get("/sessions/{uuid}/pane", response_model=PaneResponse)
async def get_pane(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    lines: int = Query(default=20, ge=5, le=100),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> PaneResponse:
    """Legge contenuto pane tmux. Rileva activity state e input prompt."""
    agent_name, workspace_id = _agent_request_scope(user)

    name = await _resolve_uuid(uuid, db, workspace_id)

    status = await tmux.get_session_status(name)
    pane_text = await tmux.capture_pane(name, last_lines=lines) if status else None
    activity = tmux.detect_activity_state(pane_text, status)

    input_prompt: str | None = None
    if activity == "needs_input" and pane_text:
        for line in reversed(pane_text.splitlines()):
            stripped = line.strip()
            if stripped:
                input_prompt = stripped
                break

    pane_lines = pane_text.splitlines()[-lines:] if pane_text else []

    _log_action(agent_name, "pane", session_uuid=uuid, session_name=name)
    return PaneResponse(
        lines=pane_lines,
        activity_state=activity or "working",
        input_prompt=input_prompt,
    )


@router.get("/sessions/{uuid}/conversation")
async def get_agent_session_conversation(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    limit: int = 20,
    role: str | None = None,
    since: str | None = None,
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Read conversation messages for a CC session. RBAC: viewer+."""
    from core.api.services.conversation_reader import read_conversation

    agent_name, workspace_id = _agent_request_scope(user)

    result = await get_session_row_by_uuid(
        uuid, db, workspace_id=workspace_id
    )
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")
    _, row = result
    if (row.get("provider") or "claude") != "claude":
        return {"conversation_id": None, "messages": []}
    if not row.get("conversation_id"):
        return {"conversation_id": None, "messages": []}
    try:
        messages = await read_conversation(
            row["conversation_id"], limit=limit, role=role, since=since
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _log_action(
        agent_name, "conversation", session_uuid=uuid, session_name=row.get("name")
    )
    return {
        "conversation_id": row["conversation_id"],
        "messages": messages or [],
    }


@router.get("/sessions/{uuid}/tasks")
async def get_session_cc_tasks(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Read Claude Code native tasks for a session (from ~/.claude/tasks/).

    Returns tasks sorted by numeric id. Returns empty list if no tasks dir found.
    """
    from core.api.services.cc_tasks_reader import read_cc_tasks

    agent_name, workspace_id = _agent_request_scope(user)

    result = await get_session_row_by_uuid(
        uuid, db, workspace_id=workspace_id
    )
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")
    _, row = result
    if (row.get("provider") or "claude") != "claude":
        return {"conversation_id": None, "tasks": []}

    if not row.get("conversation_id"):
        return {"conversation_id": None, "tasks": []}

    try:
        tasks = await read_cc_tasks(row["conversation_id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _log_action(agent_name, "cc_tasks", session_uuid=uuid, session_name=row.get("name"))
    return {
        "conversation_id": row["conversation_id"],
        "tasks": tasks or [],
    }


@router.post("/sessions/{uuid}/input", status_code=200)
async def send_input(
    body: InputBody,
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Invia risposta safe a prompt di approvazione (whitelist: y/n/Allow/Deny/Enter/Escape)."""
    agent_name, workspace_id = _agent_request_scope(user)

    name = await _resolve_uuid(uuid, db, workspace_id)

    # Enter and Escape are special tmux keys — use send_keys_raw (no added Enter)
    if body.response in ("Escape", "Enter"):
        success = await tmux.send_keys_raw(name, body.response)
    else:
        success = await tmux.send_keys(name, body.response)

    if not success:
        _log_action(
            agent_name,
            "input",
            session_uuid=uuid,
            session_name=name,
            payload=body.response,
            result="error",
            detail="send_keys failed",
        )
        raise HTTPException(status_code=500, detail="Failed to send input")

    _log_action(
        agent_name, "input", session_uuid=uuid, session_name=name, payload=body.response
    )
    return {"status": "sent", "response": body.response}


@router.post("/sessions/{uuid}/exec", status_code=200)
async def exec_input(
    body: ExecBody,
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Invia input arbitrario al terminale.

    raw=False: send_keys (testo + Enter automatico)
    raw=True: send_keys_raw (tmux key sequences, es. C-c, Escape)

    /exec conta doppio nel rate limiter.
    Circuit breaker: DevX limited to 10 exec actions per session per hour (429 if exceeded).
    Newline strippati per prevenire command splitting in tmux.
    """
    agent_name, workspace_id = _agent_request_scope(user, cost=2)

    # Resolve ownership before reading the global audit log. Otherwise a foreign
    # session UUID could be distinguished through the circuit-breaker response.
    name = await _resolve_uuid(uuid, db, workspace_id)

    # Circuit breaker: block DevX auto-response loops (10 exec/session/hour)
    await _circuit_breaker_check(agent_name, uuid, db)

    # Strip newlines — in tmux, \\n acts as Enter, splitting the command (security risk)
    safe_text = body.text.replace("\n", "").replace("\r", "")
    if not safe_text:
        raise HTTPException(
            status_code=422, detail="Text is empty after newline stripping"
        )

    if body.raw:
        success = await tmux.send_keys_raw(name, safe_text)
        # raw=True: log hash+length (not plaintext)
        text_hash = hashlib.sha256(safe_text.encode()).hexdigest()[:16]
        log_payload = json.dumps(
            {"length": len(safe_text), "sha256_prefix": text_hash, "raw": True}
        )
    else:
        success = await tmux.send_keys(name, safe_text)
        log_payload = json.dumps({"text": safe_text[:200], "raw": False})

    if not success:
        _log_action(
            agent_name,
            "exec",
            session_uuid=uuid,
            session_name=name,
            payload=log_payload,
            result="error",
            detail="send_keys failed",
        )
        raise HTTPException(status_code=500, detail="Failed to exec input")

    _log_action(
        agent_name, "exec", session_uuid=uuid, session_name=name, payload=log_payload
    )
    return {"status": "sent", "raw": body.raw, "length": len(safe_text)}


@router.post("/sessions/{uuid}/complete", status_code=200)
@_agent_session_operation("complete")
async def complete_session(
    uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    user: UserInfo = Depends(get_agent_user),
) -> dict:
    """Completa sessione: stampa completed_at, esce da Claude Code, killa tmux.

    Preserva session_costs history (a differenza di DELETE che rimuove tutto).
    Restituisce recap (cost_usd, tokens, working_seconds).
    """
    # Destructive action: same double charge as arbitrary terminal input.
    agent_name, workspace_id = _agent_request_scope(user, cost=2)

    # Resolve the tenant-owned row on a read connection. Never hold the single
    # SQLite writer while tmux/provider shutdown performs external I/O.
    async with acquire_db() as db:
        name = await _resolve_uuid(uuid, db, workspace_id)
        cursor = await db.execute(
            "SELECT conversation_id, working_seconds, project_slug, provider "
            "FROM sessions_meta "
            "WHERE name = ? AND session_uuid = ? AND workspace_id = ?",
            (name, uuid, workspace_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            await _require_agent_project_path(db, user, row["project_slug"])
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    conv_id = row["conversation_id"]
    session_provider = row["provider"] or "claude"
    provider_config = get_provider(session_provider)

    if session_provider == "claude" and not conv_id:
        claude_pid = await tmux.get_cli_pid(
            name, process_names=provider_config.process_names
        )
        if claude_pid:
            conv_id = claude_metrics.detect_conversation_by_pid(claude_pid)
        if not conv_id:
            pane_start = await tmux.get_pane_start_time(name)
            if pane_start:
                conv_id = claude_metrics.detect_conversation_for_session(
                    pane_start,
                    cwd=resolve_project_path(row["project_slug"] if row else None),
                )

    now = datetime.now(timezone.utc).isoformat()

    # Audit BEFORE destructive ops
    _log_action(agent_name, "complete", session_uuid=uuid, session_name=name)

    # Exit Claude Code gracefully if running
    status = await tmux.get_session_status(name)
    if status in provider_config.process_names:
        for step in provider_config.exit_sequence:
            await tmux.send_keys_raw(name, step.key)
            await asyncio.sleep(step.delay_after)

    if not await tmux.kill_session(name):
        _log_action(
            agent_name,
            "complete",
            session_uuid=uuid,
            session_name=name,
            result="error",
            detail="tmux kill failed",
        )
        raise HTTPException(status_code=500, detail="Failed to kill session")

    cost_row = None
    async with write_db(label="agent.complete_session") as db:
        current = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta "
                "WHERE name = ? AND session_uuid = ? AND workspace_id = ?",
                (name, uuid, workspace_id),
            )
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=409, detail="Session changed during completion")

        if conv_id:
            await db.execute(
                "UPDATE session_costs SET completed_at = ? "
                "WHERE workspace_id = ? AND conversation_id = ? AND session_name = ?",
                (now, workspace_id, conv_id, name),
            )
            cursor = await db.execute(
                "SELECT cost_usd, input_tokens, output_tokens, message_count "
                "FROM session_costs "
                "WHERE workspace_id = ? AND conversation_id = ? AND session_name = ?",
                (workspace_id, conv_id, name),
            )
            cost_row = await cursor.fetchone()
            await db.execute(
                "UPDATE session_costs SET session_name = NULL "
                "WHERE workspace_id = ? AND conversation_id = ? "
                "AND session_name = ? AND completed_at IS NOT NULL",
                (workspace_id, conv_id, name),
            )

        await db.execute(
            "DELETE FROM sessions_meta "
            "WHERE name = ? AND session_uuid = ? AND workspace_id = ?",
            (name, uuid, workspace_id),
        )

    # Build recap from the row read before it was detached.
    recap: dict = {"conversation_id": conv_id, "completed_at": now}
    if cost_row:
        recap.update(
            {
                "cost_usd": round(cost_row["cost_usd"], 4),
                "input_tokens": cost_row["input_tokens"] or 0,
                "output_tokens": cost_row["output_tokens"] or 0,
                "message_count": cost_row["message_count"],
                "working_seconds": row["working_seconds"] or 0,
            }
        )

    logger.info(
        "Agent %s completed session %s (uuid=%s, conv=%s)",
        agent_name,
        name,
        uuid,
        conv_id,
    )
    return {"status": "completed", **recap}


# ===========================================================================
# PR Workflow tools (agents call pr_service directly)
# ===========================================================================


@router.post("/pr/start_branch/{task_id}")
async def agent_start_branch(
    task_id: str,
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Create git worktree and branch for a task. Idempotent.

    After calling submit_pr, do not make further commits.
    The PR is under human review. Wait for get_pr_status to return
    status='merged' or status='closed' before continuing.
    """
    from core.api.services import pr_service
    from core.api.services.git_ops import GitOpsError

    agent_name, workspace_id = _agent_request_scope(user)

    try:
        result = await pr_service.start_branch_short_write(
            task_id, db, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except GitOpsError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    _log_action(agent_name, "pr_start_branch", payload=task_id)
    return result


@router.post("/pr/submit/{task_id}")
async def agent_submit_pr(
    task_id: str,
    body: dict,
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Submit branch for human review. Calculates diff, moves PR to 'open'.
    Fails with 422 if no commits on branch.

    Body: { "title": str, "body"?: str }
    """
    from core.api.services import pr_service
    from core.api.services.git_ops import GitOpsError

    agent_name, workspace_id = _agent_request_scope(user)

    title = body.get("title")
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    pr_body = body.get("body", "")

    try:
        result = await pr_service.submit_pr(
            task_id,
            title,
            pr_body,
            db,
            submitted_by=user.user_id,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except GitOpsError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    _log_action(agent_name, "pr_submit", payload=task_id)
    return result


@router.get("/pr/status/{task_id}")
async def agent_get_pr_status(
    task_id: str,
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get current PR status including diff_summary and worktree_path."""
    from core.api.services import pr_service

    agent_name, workspace_id = _agent_request_scope(user)

    try:
        result = await pr_service.get_pr_status(
            task_id, db, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    _log_action(agent_name, "pr_status", payload=task_id)
    return result


@router.post("/pr/abandon/{task_id}")
async def agent_abandon_pr(
    task_id: str,
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Close PR without merge. Task returns to in_progress for rework."""
    from core.api.services import pr_service

    agent_name, workspace_id = _agent_request_scope(user)

    try:
        result = await pr_service.close_pr(
            task_id,
            "abandoned by agent",
            db,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    _log_action(agent_name, "pr_abandon", payload=task_id)
    return result


@router.patch("/pr/update/{task_id}")
async def agent_update_pr(
    task_id: str,
    body: dict,
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Update PR title or body without reopening.

    Body: { "title"?: str, "body"?: str }
    """
    from core.api.services import pr_service

    agent_name, workspace_id = _agent_request_scope(user)

    try:
        result = await pr_service.update_pr(
            task_id,
            db,
            title=body.get("title"),
            body=body.get("body"),
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    _log_action(agent_name, "pr_update", payload=task_id)
    return result
