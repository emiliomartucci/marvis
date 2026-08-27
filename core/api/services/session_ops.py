# v1.0.0 - 2026-02-26 - Shared session business logic (used by sessions + agent routers)
from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import aiosqlite

from core.api.db import acquire_db, write_db
from core.api.services.access_grants import require_unique_project_workspace
from core.api.services import claude_metrics, opencode_sessions, tmux
from core.api.services.project_paths import (
    candidate_project_paths,
    resolve_project_access_paths,
    resolve_project_path,
)
from core.api.services.providers import (
    ALL_KNOWN_PROCESS_NAMES,
    ProviderConfig,
    build_start_command,
    get_provider,
)
from core.api.services.session_catalog import (
    get_model_definition,
    get_permission_preset_definition,
    resolve_launch_directory,
)

logger = logging.getLogger(__name__)

# Columns needed for AgentSessionView construction — matches DB_COLUMNS in sessions.py
_SESSION_COLUMNS = (
    "name, workspace_id, display_name, pinned, sort_order, group_name, project_slug, session_uuid, "
    "created_at, last_active, conversation_id, hibernated, model, launch_model, "
    # PR3: rename 088 — source last_context_pct from last_context_pct_real (true ratio)
    "permission_preset, bootstrap_message, last_context_pct_real AS last_context_pct, "
    "last_cost_usd, last_message_count, auto_hibernate_minutes, working_seconds, "
    "provider, agent_managed"
)


@dataclass(frozen=True, slots=True)
class SessionLaunchSpec:
    provider: str
    provider_config: ProviderConfig
    launch_dir: str
    model_id: str
    cli_model: str | None
    permission_preset: str | None
    start_command: str


def _provider_access_args(
    session_provider: str,
    project_slug: str | None,
    launch_dir: str,
) -> tuple[str, ...]:
    access_paths = tuple(
        path
        for path in resolve_project_access_paths(project_slug)
        if path != launch_dir
    )
    if not access_paths:
        return ()

    if session_provider in ("claude", "codex"):
        flag = "--add-dir"
    elif session_provider == "gemini":
        flag = "--include-directories"
    else:
        return ()

    return tuple(f"{flag} {shlex.quote(path)}" for path in access_paths)


def build_session_start_spec(
    provider: str | None,
    project_slug: str | None,
    launch_model: str | None = None,
    permission_preset: str | None = None,
    resume_session_id: str | None = None,
    *,
    session_name: str | None = None,
    theme_mode: Literal["light", "dark"] | None = None,
) -> SessionLaunchSpec:
    session_provider = provider or "claude"
    provider_config = get_provider(session_provider)
    model_def = get_model_definition(session_provider, launch_model)
    preset_def = get_permission_preset_definition(session_provider, permission_preset)
    project_path = resolve_project_path(project_slug)
    launch_dir = resolve_launch_directory(session_provider, project_path)
    extra_cli_args = model_def.launch_args + _provider_access_args(
        session_provider,
        project_slug,
        launch_dir,
    )
    if session_provider == "opencode" and resume_session_id:
        extra_cli_args = (
            f"--session {shlex.quote(resume_session_id)}",
        ) + extra_cli_args
    start_command = build_start_command(
        provider_config,
        launch_dir,
        model=model_def.cli_model,
        opencode_config=preset_def.config_override if preset_def else None,
        opencode_theme_mode=theme_mode if session_provider == "opencode" else None,
        session_name=session_name if session_provider == "opencode" else None,
        extra_cli_args=extra_cli_args,
    )
    return SessionLaunchSpec(
        provider=session_provider,
        provider_config=provider_config,
        launch_dir=launch_dir,
        model_id=model_def.id,
        cli_model=model_def.cli_model,
        permission_preset=preset_def.id if preset_def else None,
        start_command=start_command,
    )


async def get_session_row_by_uuid(
    session_uuid: str,
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
) -> tuple[str, dict] | None:
    """Return (name, row_dict) for a session UUID, or None if not found.

    Targeted O(1) lookup — does NOT call _sync_sessions.
    """
    cursor = await db.execute(
        f"SELECT {_SESSION_COLUMNS} FROM sessions_meta "
        "WHERE session_uuid = ? AND workspace_id = ?",
        (session_uuid, workspace_id),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return row["name"], dict(row)


async def get_live_session_data(name: str, provider: str | None = None) -> dict:
    """Fetch live tmux status, activity state, and process metrics for one session.

    Lightweight: only queries tmux for the target session, not all sessions.
    """
    alive = await tmux.session_exists(name)
    if not alive:
        return {"status": None, "activity_state": None, "cpu_pct": None, "ram_mb": None}

    provider_config = get_provider(provider)
    status = await tmux.get_session_status(name)
    is_cli_active = status in ALL_KNOWN_PROCESS_NAMES
    pane_text = await tmux.capture_pane(name, last_lines=20) if is_cli_active else None
    activity = tmux.detect_activity_state(pane_text, status, provider=provider)

    cpu_pct: float | None = None
    ram_mb: float | None = None
    if is_cli_active:
        pid = await tmux.get_cli_pid(name, process_names=provider_config.process_names)
        if pid:
            process_metrics = await tmux.get_process_metrics(pid)
            if process_metrics is not None:
                cpu_raw, rss_kb = process_metrics
                cpu_pct = round(cpu_raw, 1)
                ram_mb = round(rss_kb / 1024, 1)

    return {
        "status": status,
        "activity_state": activity,
        "cpu_pct": cpu_pct,
        "ram_mb": ram_mb,
    }


async def hibernate_session_core(
    name: str,
    conv_id: str | None = None,
    provider: str | None = None,
    project_slug: str | None = None,
    *,
    workspace_id: str,
    session_uuid: str,
) -> dict:
    """Core hibernate business logic: send /exit, snapshot metrics, update DB.

    Shared between sessions router and agent router.
    Returns dict with status and conversation_id.
    """
    # The agent route checks ownership before scheduling this background job,
    # but filesystem ownership can change before the job actually runs. Recheck
    # at the worker boundary immediately before any project path is resolved.
    if project_slug:
        async with acquire_db() as ownership_db:
            await require_unique_project_workspace(
                ownership_db,
                project_slug=project_slug,
                workspace_id=workspace_id,
            )
    provider_config = get_provider(provider)
    is_claude = (provider or "claude") == "claude"

    # Detect conversation_id if not provided
    if is_claude and not conv_id:
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
                    cwd=resolve_project_path(project_slug),
                )
    elif (provider or "claude") == "opencode" and not conv_id:
        launch_spec = build_session_start_spec(provider, project_slug)
        async with acquire_db() as db:
            cursor = await db.execute(
                "SELECT created_at FROM sessions_meta "
                "WHERE name = ? AND session_uuid = ? AND workspace_id = ?",
                (name, session_uuid, workspace_id),
            )
            row = await cursor.fetchone()
        conv_id = await asyncio.to_thread(
            opencode_sessions.find_session_id_for_created_at,
            launch_spec.launch_dir,
            row["created_at"] if row else None,
        )

    # Snapshot metrics before hibernating (Claude only)
    metrics = None
    if is_claude and conv_id:
        conv_cwd = await asyncio.to_thread(
            claude_metrics.find_conversation_cwd,
            conv_id,
            candidate_project_paths(project_slug),
        )
        if conv_cwd:
            metrics = await asyncio.to_thread(
                claude_metrics.find_conversation_by_id, conv_id, conv_cwd
            )

    # Send provider-specific exit sequence
    status = await tmux.get_session_status(name)
    if status in provider_config.process_names:
        for step in provider_config.exit_sequence:
            await tmux.send_keys_raw(name, step.key)
            await asyncio.sleep(step.delay_after)

    # Update DB
    now = datetime.now(timezone.utc).isoformat()
    updates = ["hibernated = 1", "hibernated_at = ?"]
    params: list = [now]
    if conv_id:
        updates.append("conversation_id = ?")
        params.append(conv_id)
    if metrics:
        # PR3: drop the legacy last_context_pct write — the real/scaled
        # columns (migration 087) are populated by the maintenance loop.
        updates.extend(
            [
                "model = ?",
                "last_cost_usd = ?",
                "last_message_count = ?",
            ]
        )
        params.extend(
            [
                metrics.model,
                metrics.cost_usd,
                metrics.message_count,
            ]
        )
    params.extend((name, workspace_id))
    params.append(session_uuid)
    async with write_db(label="agent.hibernate_session") as db:
        await db.execute(
            f"UPDATE sessions_meta SET {', '.join(updates)} "
            "WHERE name = ? AND workspace_id = ? AND session_uuid = ?",
            params,
        )

    logger.info(
        "Session hibernated: %s (conversation=%s, provider=%s)",
        name,
        conv_id,
        provider or "claude",
    )
    return {"status": "hibernated", "conversation_id": conv_id}
