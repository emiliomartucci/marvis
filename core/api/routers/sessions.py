# v1.14.0 - 2026-04-14 - send_message_to_session uses get_write_db (refactor batch 4/6)
from __future__ import annotations

import asyncio
import logging
import shlex
import time as _time
import re
import uuid as uuid_mod
from datetime import datetime, timezone
from functools import wraps
from inspect import signature as inspect_signature
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Request

from core.api.config import settings
from core.api.db import acquire_db, get_db, get_write_db, write_db


def _get_session_manager():
    """Lazy import to avoid circular dependency with terminal.py."""
    from core.api.terminal import session_manager

    return session_manager


from core.api.models import (
    SendMessageBody,
    SessionCatalogModel,
    SessionCatalogProvider,
    SessionCatalogResponse,
    SessionCreate,
    SessionInfo,
    SessionMetricsResponse,
    SessionPermissionPreset,
    SessionReorder,
    SessionStateUpdate,
    SessionUpdate,
    UserInfo,
)
from core.api.rbac import require_scope
from core.api.security import (
    get_current_user,
    get_current_user_or_agent,
)
from core.api.services import session_state as session_state_svc
from core.api.services import opencode_sessions
from core.api.services import claude_metrics, codex_metrics, tmux
from core.api.services.metrics_providers import get_metrics_provider
from core.api.services.project_paths import candidate_project_paths, resolve_project_path
from core.api.services.providers import (
    ALL_KNOWN_PROCESS_NAMES,
    build_start_command,
    get_provider,
    is_binary_available,
)
from core.api.services.session_catalog import list_catalog_models, list_provider_definitions
from core.api.services.session_ops import build_session_start_spec
from core.api.services.session_operation_leases import (
    SessionOperationBusy,
    SessionOperationMissing,
    acquire_session_operation_lease,
    release_session_operation_lease,
    session_operation_lease,
)

_CONVERSATION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UUID_V4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

logger = logging.getLogger(__name__)

_GLOBAL_AGENT_SESSION_VIEWERS = {"marvisx", "console-api", "marvis-local"}


async def _table_has_columns(
    db: aiosqlite.Connection,
    table_name: str,
    required_columns: set[str],
) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await cursor.fetchall()
    return required_columns.issubset({row["name"] for row in rows})


async def _tmux_user_env_for_session(
    db: aiosqlite.Connection,
    current_user: UserInfo,
) -> dict[str, str] | None:
    if not (settings.multi_tenant_enabled or settings.uid_isolation_enabled):
        return None

    user_id = current_user.user_id or current_user.username
    env = {
        "DEPLOY_MODE": settings.deploy_mode,
        "TENANT_SLUG": settings.deploy_mode,
        "USER_ID": user_id,
    }

    if not settings.uid_isolation_enabled:
        return env

    has_uid_columns = await _table_has_columns(
        db,
        "users",
        {"uid_index", "assigned_uid"},
    )
    if not has_uid_columns:
        raise HTTPException(
            status_code=500,
            detail="UID isolation is enabled but users.uid_index/assigned_uid are missing",
        )

    async with db.execute(
        "SELECT uid_index, assigned_uid FROM users WHERE id = ? AND workspace_id = ?",
        [user_id, _workspace_for_user(current_user)],
    ) as cursor:
        row = await cursor.fetchone()

    if row is None or not row["uid_index"] or not row["assigned_uid"]:
        raise HTTPException(
            status_code=403,
            detail="User has no UID isolation mapping",
        )

    uid_index = int(row["uid_index"])
    assigned_uid = int(row["assigned_uid"])
    if uid_index < 1 or uid_index > settings.uid_pool_size:
        raise HTTPException(status_code=500, detail="User UID index is out of range")
    if assigned_uid != settings.uid_pool_base + uid_index - 1:
        raise HTTPException(status_code=500, detail="User UID assignment is inconsistent")

    username = f"{settings.uid_pool_prefix}-{uid_index:02d}"
    env["USER_UID_INDEX"] = str(uid_index)
    env["USER_UID"] = str(assigned_uid)
    env["USER_HOME"] = f"/data/users/{username}"
    return env


def _get_system_uptime_seconds() -> float:
    """Read system uptime from /proc/uptime. Returns 0.0 on error."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _created_epoch_from_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


router = APIRouter(prefix="/sessions", tags=["sessions"])

# Sessions cache (TTL-based, shared full-list — filtering applied at endpoint level)
_sessions_cache: list | None = None
_sessions_cache_ts: float = 0.0
_sessions_cache_refresh_task: asyncio.Task[list[SessionInfo]] | None = None
_sessions_cache_generation: int = 0
_sessions_cache_last_state = "empty"
_sessions_cache_pending_patch_state: str | None = None
_sessions_last_sync_timings: dict[str, Any] | None = None
_sessions_cache_lock = asyncio.Lock()
_CACHE_TTL = (
    15  # seconds (was 2.5 — too short, caused ~500ms _sync_sessions on every request)
)
_CACHE_STALE_TTL = 120  # seconds: serve stale during background refresh


def _invalidate_sessions_cache() -> None:
    """Force next list_sessions call to re-sync from tmux + DB."""
    global _sessions_cache, _sessions_cache_ts, _sessions_cache_last_state
    global _sessions_cache_refresh_task, _sessions_cache_generation
    global _sessions_cache_pending_patch_state
    _sessions_cache = None
    _sessions_cache_ts = 0.0
    _sessions_cache_generation += 1
    _sessions_cache_refresh_task = None
    _sessions_cache_last_state = "invalidated"
    _sessions_cache_pending_patch_state = None


def _patch_sessions_cache_activity_state(
    session_name: str,
    state: str,
    workspace_id: str | None = None,
) -> bool:
    """Patch a state-only update into the existing full-list cache.

    The cache is unfiltered and RBAC is applied after reads. This helper only
    updates an existing cached session and never creates a cache entry from a
    state event, so it cannot broaden visibility.
    """
    global _sessions_cache_last_state, _sessions_cache_pending_patch_state
    if _sessions_cache is None:
        _sessions_cache_last_state = "state_patch_no_cache"
        return False
    for session in _sessions_cache:
        workspace_matches = workspace_id is None or session.workspace_id == workspace_id
        if (
            workspace_id == "ws_default"
            and _allow_legacy_local_session_schema()
            and session.workspace_id is None
        ):
            workspace_matches = True
        if session.name == session_name and workspace_matches:
            session.activity_state = state
            _sessions_cache_last_state = "state_patched"
            _sessions_cache_pending_patch_state = "state_patched"
            return True
    _sessions_cache_last_state = "state_patch_miss"
    return False


def _record_sessions_control_event(
    request: Request | None,
    *,
    workspace_id: str,
    kind: str,
    duration_ms: float,
    metadata: dict[str, Any] | None = None,
) -> None:
    if request is None:
        return
    app = getattr(request, "app", None)
    app_state = getattr(app, "state", None)
    if app_state is None:
        return
    try:
        from core.api.services.terminal_metrics import TerminalMetricsCollector
    except Exception:
        return
    if workspace_id == "ws_default":
        collector = getattr(app_state, "terminal_metrics", None)
    else:
        collectors = getattr(app_state, "terminal_metrics_by_workspace", None)
        if not isinstance(collectors, dict):
            collectors = {}
            app_state.terminal_metrics_by_workspace = collectors
        collector = collectors.get(workspace_id)
        if not isinstance(collector, TerminalMetricsCollector):
            collector = TerminalMetricsCollector()
            collectors[workspace_id] = collector
    if isinstance(collector, TerminalMetricsCollector):
        collector.record_sessions_control_event(
            kind=kind,
            duration_ms=duration_ms,
            metadata=metadata,
        )


async def _refresh_sessions_cache_from_pool() -> list[SessionInfo]:
    async with acquire_db() as db:
        return await _sync_sessions_read_only(db)


def _finish_sessions_cache_refresh(
    task: asyncio.Task[list[SessionInfo]],
    generation: int,
) -> None:
    global _sessions_cache, _sessions_cache_ts, _sessions_cache_refresh_task
    if generation != _sessions_cache_generation:
        return
    if _sessions_cache_refresh_task is task:
        _sessions_cache_refresh_task = None
    try:
        sessions = task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.warning("sessions cache refresh failed", exc_info=True)
        return
    _sessions_cache = sessions
    _sessions_cache_ts = _time.monotonic()


def _start_sessions_cache_refresh_unlocked(
    db: aiosqlite.Connection,
    *,
    detached: bool,
) -> asyncio.Task[list[SessionInfo]]:
    global _sessions_cache_refresh_task
    if _sessions_cache_refresh_task and not _sessions_cache_refresh_task.done():
        return _sessions_cache_refresh_task
    generation = _sessions_cache_generation
    coro = _refresh_sessions_cache_from_pool() if detached else _sync_sessions_read_only(db)
    task = asyncio.create_task(coro)
    task.add_done_callback(
        lambda completed, task_generation=generation: _finish_sessions_cache_refresh(
            completed,
            task_generation,
        )
    )
    _sessions_cache_refresh_task = task
    return task


async def _get_sessions_cached(
    db: aiosqlite.Connection,
    workspace_id: str | None = None,
) -> list[SessionInfo]:
    """Return shared sessions with singleflight + stale-while-revalidate.

    The legacy cache is used only by explicit local single-user mode. Remote
    callers bypass it and execute a workspace-scoped sync so cached objects from
    one workspace can never be reused by another.
    """
    global _sessions_cache_last_state
    if workspace_id is not None:
        _sessions_cache_last_state = "workspace_scoped"
        return await _sync_sessions_read_only(db, workspace_id=workspace_id)

    now = _time.monotonic()
    if _sessions_cache is not None and (now - _sessions_cache_ts) < _CACHE_TTL:
        _sessions_cache_last_state = "hit"
        return _sessions_cache

    async with _sessions_cache_lock:
        now = _time.monotonic()
        if _sessions_cache is not None and (now - _sessions_cache_ts) < _CACHE_TTL:
            _sessions_cache_last_state = "hit_after_lock"
            return _sessions_cache

        if (
            _CACHE_TTL > 0
            and _sessions_cache is not None
            and (now - _sessions_cache_ts) < _CACHE_STALE_TTL
        ):
            _start_sessions_cache_refresh_unlocked(db, detached=True)
            _sessions_cache_last_state = "stale_background_refresh"
            return _sessions_cache

        refresh_task = _start_sessions_cache_refresh_unlocked(db, detached=False)
        _sessions_cache_last_state = "miss_wait"

    return await asyncio.shield(refresh_task)


def _can_view_all_sessions(current_user: UserInfo) -> bool:
    if current_user.system_role in ("admin", "super_admin"):
        return True
    canonical_username = current_user.username.removeprefix("agent:")
    return (
        current_user.user_type == "agent"
        and canonical_username in _GLOBAL_AGENT_SESSION_VIEWERS
    )


def _workspace_for_user(current_user: UserInfo) -> str:
    workspace_id = (current_user.workspace_id or "").strip()
    if workspace_id:
        return workspace_id
    if _allow_legacy_local_session_schema():
        return "ws_default"
    raise HTTPException(status_code=403, detail="Session not found")


def _allow_legacy_local_session_schema() -> bool:
    return settings.deploy_mode == "core" and not settings.multi_tenant_enabled


def _broadcast_session_event(
    event: str,
    *,
    workspace_id: str,
    **extras: Any,
):
    """Keep the historical local manager call while scoping remote broadcasts."""
    manager = _get_session_manager()
    if _allow_legacy_local_session_schema():
        return manager.broadcast_session_event(event, **extras)
    return manager.broadcast_session_event(
        event, workspace_id=workspace_id, **extras
    )


async def _sessions_have_workspace(db: aiosqlite.Connection) -> bool:
    return await _table_has_columns(db, "sessions_meta", {"workspace_id"})


async def _require_session_row(
    db: aiosqlite.Connection,
    current_user: UserInfo,
    *,
    name: str | None = None,
    session_uuid: str | None = None,
) -> aiosqlite.Row:
    """Resolve one session inside the authenticated workspace or return 404."""
    workspace_id = _workspace_for_user(current_user)
    if (name is None) == (session_uuid is None):
        raise ValueError("exactly one session identifier is required")
    column = "name" if name is not None else "session_uuid"
    value = name if name is not None else session_uuid
    has_workspace = await _sessions_have_workspace(db)
    columns = DB_COLUMNS if has_workspace else "*"
    if has_workspace and not _allow_legacy_local_session_schema():
        cursor = await db.execute(
            f"SELECT {columns} FROM sessions_meta "
            f"WHERE {column} = ? AND workspace_id = ?",
            (value, workspace_id),
        )
    elif _allow_legacy_local_session_schema():
        cursor = await db.execute(
            f"SELECT {columns} FROM sessions_meta WHERE {column} = ?", (value,)
        )
    else:
        raise HTTPException(status_code=503, detail="Session registry unavailable")
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


def _session_operation(operation: str):
    """Serialize one Console lifecycle endpoint across API processes."""

    def decorator(func):
        function_signature = inspect_signature(func)

        @wraps(func)
        async def wrapped(*args, **kwargs):
            bound = function_signature.bind_partial(*args, **kwargs)
            user: UserInfo = bound.arguments["_user"]
            name: str = bound.arguments["name"]
            workspace_id = _workspace_for_user(user)
            route_db = bound.arguments.get("db")

            if route_db is None:
                async with acquire_db() as read_db:
                    row = await _require_session_row(
                        read_db,
                        user,
                        name=name,
                    )
                    session_uuid = str(row["session_uuid"] or "").strip()
                if not session_uuid:
                    raise HTTPException(409, "Session generation unavailable")
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

            row = await _require_session_row(route_db, user, name=name)
            session_uuid = str(row["session_uuid"] or "").strip()
            if not session_uuid:
                raise HTTPException(409, "Session generation unavailable")
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


async def _require_project_workspace(
    db: aiosqlite.Connection,
    current_user: UserInfo,
    project_slug: str | None,
) -> None:
    if not project_slug:
        return
    workspace_id = _workspace_for_user(current_user)
    try:
        row = await (
            await db.execute(
                "SELECT COUNT(DISTINCT workspace_id) AS workspace_count, "
                "MAX(CASE WHEN workspace_id = ? THEN 1 ELSE 0 END) AS owned "
                "FROM workspace_projects WHERE project_slug = ?",
                (workspace_id, project_slug),
            )
        ).fetchone()
    except aiosqlite.Error:
        if _allow_legacy_local_session_schema():
            return
        raise HTTPException(status_code=503, detail="Project registry unavailable")
    if (
        row is None
        or int(row["workspace_count"] or 0) != 1
        or not bool(row["owned"])
    ):
        raise HTTPException(status_code=404, detail="Project not found")


DB_COLUMNS = (
    "name, workspace_id, display_name, pinned, sort_order, group_name, project_slug, session_uuid, "
    "created_at, last_active, conversation_id, hibernated, model, launch_model, "
    "permission_preset, theme_mode, bootstrap_message, "
    # PR3: rename 088 — source `last_context_pct` (API field) from
    # `last_context_pct_real` (true ratio). `last_context_pct_legacy`
    # column survives read-only for forensics.
    "last_context_pct_real AS last_context_pct, last_context_pct_legacy, "
    "last_cost_usd, last_message_count, auto_hibernate_minutes, working_seconds, agent_managed, "
    "owner_id, provider, "
    # PR2 dual metrics (migration 087)
    "last_context_pct_real, last_context_pct_scaled, "
    "last_cost_conversation_usd, last_cost_session_usd, last_cost_session_incomplete, "
    "last_input_tokens, last_output_tokens, last_reasoning_tokens, "
    "working_seconds_msg, metrics_refreshed_at, pricing_version, "
    # PR4 shadow cost (migration 089)
    "last_cost_conversation_equivalent_usd, last_cost_session_equivalent_usd, "
    "last_cost_equivalent_pricing_version, "
    # PR1 event-driven session state (migrations 092 + 093)
    "activity_state, activity_state_updated_at"
)

# Fresh-event threshold for activity_state. If the DB-stored event timestamp
# is within this window, trust the column. Global session-list reads do not
# capture panes; stale/missing events surface as unknown instead of scraping.
_ACTIVITY_EVENT_TTL_SECS = 60.0


def _index_processes_by_parent(
    processes: dict[int, tmux.ProcessSnapshot],
) -> dict[int, list[tmux.ProcessSnapshot]]:
    by_parent: dict[int, list[tmux.ProcessSnapshot]] = {}
    for process in processes.values():
        by_parent.setdefault(process.parent_pid, []).append(process)
    return by_parent


def _session_process_snapshot(
    session: SessionInfo,
    pane_pids: dict[str, int],
    processes_by_parent: dict[int, list[tmux.ProcessSnapshot]],
) -> tmux.ProcessSnapshot | None:
    pane_pid = pane_pids.get(session.name)
    if pane_pid is None:
        return None

    children = processes_by_parent.get(pane_pid, [])
    if not children:
        return None

    try:
        process_names = get_provider(session.provider).process_names
    except ValueError:
        process_names = ALL_KNOWN_PROCESS_NAMES
    for process_name in process_names:
        for child in children:
            if child.command == process_name:
                return child
    return None


def _detect_project_from_path(cwd: str) -> str | None:
    """Match a filesystem path to a project slug using the project index.

    The shared workspace root is every session's default launch cwd (the
    cross-provider bootstrap dir). It is NOT a project: inferring the repo's own
    project from it mislabels every session that launches there, and is exactly
    how the 2026-06-12 restore re-tagged recovered sessions to the repo's own
    project slug. Treat the workspace root itself as unknown → NULL; real project
    repos and the ``/data/projects`` metadata paths below still match.
    """
    from core.api.routers.projects import (
        _build_project_index,
        _project_index,
        _index_built_at,
        _INDEX_TTL,
    )
    import time as _t

    workspace_root = (settings.effective_workspace_root or "").rstrip("/")
    if workspace_root and cwd.rstrip("/") == workspace_root:
        return None

    if _t.monotonic() - _index_built_at > _INDEX_TTL:
        _build_project_index()
    for slug, entry in _project_index.items():
        path = (
            str(entry.repo_path)
            if entry.repo_path
            else str(entry.metadata_path.resolve())
        )
        if cwd == path or cwd.startswith(path + "/"):
            return slug
    return None


def _resolve_conversation_cwd(
    conversation_id: str | None, project_slug: str | None
) -> str | None:
    """Find the Claude JSONL cwd for a stored conversation_id.

    Primary lookup uses the session project path. Workspace remains as a legacy
    fallback for rows that were contaminated before project-aware lookup landed.
    """
    if not conversation_id or not _CONVERSATION_ID_RE.match(conversation_id):
        return None
    return claude_metrics.find_conversation_cwd(
        conversation_id,
        candidate_project_paths(project_slug),
    )


def _project_bootstrap_message(project_slug: str | None) -> str | None:
    _ = project_slug
    return None


def _row_value(row: aiosqlite.Row | None, key: str) -> Any:
    if row is None or key not in row.keys():
        return None
    return row[key]


async def _fetch_session_meta_row(
    db: aiosqlite.Connection,
    name: str,
    workspace_id: str | None = None,
) -> aiosqlite.Row | None:
    has_workspace = await _sessions_have_workspace(db)
    columns = DB_COLUMNS if has_workspace else "*"
    if (
        workspace_id is not None
        and has_workspace
        and not _allow_legacy_local_session_schema()
    ):
        cursor = await db.execute(
            f"SELECT {columns} FROM sessions_meta WHERE name = ? AND workspace_id = ?",
            (name, workspace_id),
        )
    elif workspace_id is not None and not _allow_legacy_local_session_schema():
        raise HTTPException(status_code=503, detail="Session registry unavailable")
    else:
        cursor = await db.execute(
            f"SELECT {columns} FROM sessions_meta WHERE name = ?", (name,)
        )
    return await cursor.fetchone()


def _is_claimable_marvisx_orphan(
    row: aiosqlite.Row | None,
    current_user: UserInfo,
) -> bool:
    if row is None:
        return True

    owner_id = _row_value(row, "owner_id")
    current_owner_id = current_user.user_id or None
    if owner_id and owner_id != current_owner_id:
        return False

    return (
        not owner_id
        or not _row_value(row, "session_uuid")
        or not _row_value(row, "provider")
        or not (_row_value(row, "model") or _row_value(row, "launch_model"))
    )


async def _persist_session_create_metadata(
    db: aiosqlite.Connection,
    *,
    name: str,
    current_user: UserInfo,
    project_slug: str | None,
    provider_name: str,
    selected_model: str | None,
    selected_model_id: str | None,
    permission_preset: str | None,
    theme_mode: str | None,
    bootstrap_message: str | None,
) -> tuple[str, str, str]:
    workspace_id = _workspace_for_user(current_user)
    existing = await _fetch_session_meta_row(db, name, workspace_id)
    has_workspace = await _sessions_have_workspace(db)
    if existing is None and has_workspace:
        collision = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta WHERE name = ? "
                "AND (workspace_id IS NULL OR workspace_id != ?)",
                (name, workspace_id),
            )
        ).fetchone()
        if collision is not None:
            raise HTTPException(status_code=409, detail="Session already exists")
    now = datetime.now(timezone.utc).isoformat()
    session_uuid = _row_value(existing, "session_uuid") or str(uuid_mod.uuid4())
    created_at = _row_value(existing, "created_at") or now
    owner_id = current_user.user_id or None

    try:
        if has_workspace and _allow_legacy_local_session_schema() and existing:
            await db.execute(
                "UPDATE sessions_meta SET workspace_id = ? WHERE name = ?",
                (workspace_id, name),
            )
        await db.execute(
            "INSERT OR IGNORE INTO sessions_meta "
            "(name, workspace_id, session_uuid, created_at, last_active, owner_id, "
            "project_slug, provider, model, launch_model, permission_preset, "
            "theme_mode, bootstrap_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                workspace_id,
                session_uuid,
                created_at,
                now,
                owner_id,
                project_slug,
                provider_name,
                selected_model,
                selected_model_id,
                permission_preset,
                theme_mode,
                bootstrap_message,
            ),
        )
        await db.execute(
            """UPDATE sessions_meta SET
                session_uuid = COALESCE(NULLIF(session_uuid, ''), ?),
                created_at = COALESCE(created_at, ?),
                last_active = ?,
                owner_id = ?,
                project_slug = ?,
                provider = ?,
                model = ?,
                launch_model = ?,
                permission_preset = ?,
                theme_mode = ?,
                bootstrap_message = ?
            WHERE name = ? AND workspace_id = ?""",
            (
                session_uuid,
                created_at,
                now,
                owner_id,
                project_slug,
                provider_name,
                selected_model,
                selected_model_id,
                permission_preset,
                theme_mode,
                bootstrap_message,
                name,
                workspace_id,
            ),
        )
    except Exception:
        # Fallback pre-migration: provider/owner/theme columns may not exist yet.
        if has_workspace:
            await db.execute(
                "INSERT OR REPLACE INTO sessions_meta "
                "(name, workspace_id, session_uuid, created_at, last_active, owner_id, project_slug, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    workspace_id,
                    session_uuid,
                    created_at,
                    now,
                    owner_id,
                    project_slug,
                    selected_model,
                ),
            )
        elif _allow_legacy_local_session_schema():
            await db.execute(
                "INSERT OR REPLACE INTO sessions_meta "
                "(name, session_uuid, created_at, last_active, owner_id, project_slug, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    session_uuid,
                    created_at,
                    now,
                    owner_id,
                    project_slug,
                    selected_model,
                ),
            )
        else:
            raise

    return session_uuid, created_at, now


async def _process_command(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-p",
            str(pid),
            "-o",
            "args=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
        return stdout.decode("utf-8", errors="replace").strip()
    except (asyncio.TimeoutError, OSError):
        return ""


def _infer_provider_from_runtime(status: str | None, command: str) -> str | None:
    haystack = f"{status or ''} {command}".lower()
    for provider in ("opencode", "codex", "gemini", "claude"):
        if provider in haystack:
            return provider
    return None


def _infer_model_from_command(command: str) -> str | None:
    if not command:
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for idx, part in enumerate(parts):
        if part in {"-m", "--model"} and idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _infer_project_from_command(command: str) -> str | None:
    if not command:
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for idx, part in enumerate(parts):
        if part in {"--add-dir", "--include-directories"} and idx + 1 < len(parts):
            detected = _detect_project_from_path(parts[idx + 1])
            if detected:
                return detected
    return None


# --- Reconcile cleanup safety (task 018a38e2) --------------------------------
# The 2026-06-12 mass-session-death wiped the whole registry: a tmux server OOM
# made every row look "dead" in one reconcile pass, the old guard deleted them
# all (it only checked *system* uptime, not the tmux server), and restore then
# re-registered each session to the repo project inferred from ~/workspace.
# Two protections, both failing toward KEEPING rows:
#   1. Trust gate — never clean up when the tmux listing is empty or has clearly
#      collapsed (server down/restarting → listing empty or mostly absent).
#   2. Tombstone — a row is deleted only after it has been absent for
#      RECONCILE_TOMBSTONE_CYCLES consecutive *trustworthy* cycles. In-memory by
#      design: it resets on API restart, which only ever delays a deletion.
RECONCILE_TOMBSTONE_CYCLES = 3
RECONCILE_MASS_ABSENCE_MIN = 5
_LOCAL_RECONCILE_SCOPE = "__local__"
_reconcile_absent_cycles: dict[tuple[str, str], int] = {}


def _reconcile_names_to_delete(
    tmux_names: set[str],
    db_names: set[str],
    workspace_id: str | None = None,
) -> list[str]:
    """Return registry rows safe to delete this cycle; mutate the tombstone map.

    Pure decision over the live tmux names and the known registry names plus the
    module-level absence counter. A name is returned only once it has been absent
    for RECONCILE_TOMBSTONE_CYCLES consecutive trustworthy cycles. When the tmux
    listing looks untrustworthy (empty while rows exist, or a majority of rows
    gone at once) we delete nothing and freeze the counters, so a server restart
    can never march rows to deletion across the outage.
    """
    dead_names = db_names - tmux_names
    total = len(db_names)

    untrustworthy = (bool(total) and not tmux_names) or (
        len(dead_names) >= RECONCILE_MASS_ABSENCE_MIN
        and bool(total)
        and len(dead_names) > total // 2
    )
    if untrustworthy:
        if dead_names:
            logger.warning(
                "Session reconcile: untrustworthy tmux listing "
                "(%d live, %d/%d rows absent) — skipping cleanup, freezing tombstones",
                len(tmux_names),
                len(dead_names),
                total,
            )
        return []

    scope = (workspace_id or "").strip() or _LOCAL_RECONCILE_SCOPE

    # Trustworthy cycle: live names reset, vanished-from-registry names forgotten.
    # Counters from other workspaces remain untouched when the hosted
    # maintenance loop reconciles each registry in sequence.
    for key in list(_reconcile_absent_cycles):
        key_scope, name = key
        if key_scope != scope:
            continue
        if name in tmux_names or name not in db_names:
            _reconcile_absent_cycles.pop(key, None)

    to_delete: list[str] = []
    for name in dead_names:
        key = (scope, name)
        seen = _reconcile_absent_cycles.get(key, 0) + 1
        _reconcile_absent_cycles[key] = seen
        if seen >= RECONCILE_TOMBSTONE_CYCLES:
            to_delete.append(name)
    return to_delete


async def reconcile_sessions_metadata(workspace_id: str | None = None) -> int:
    """Persist tmux-only sessions and backfill missing stable metadata.

    Public session-list reads use the read-only DB pool, so they can expose a
    live tmux session that is not yet persisted. This maintenance helper runs
    out of band, performs tmux/process inspection outside the writer lock, then
    applies a small metadata-only write batch.
    """
    local_compatibility = _allow_legacy_local_session_schema()
    workspace_scope = (workspace_id or "").strip() or None
    if workspace_scope is None and not local_compatibility:
        logger.warning(
            "Skipped session reconciliation without exact workspace context"
        )
        return 0

    async with acquire_db() as db:
        columns = (
            "name, session_uuid, created_at, last_active, project_slug, "
            "provider, model, launch_model, workspace_id"
        )
        if workspace_scope is not None:
            cursor = await db.execute(
                f"SELECT {columns} FROM sessions_meta WHERE workspace_id = ?",
                (workspace_scope,),
            )
        else:
            cursor = await db.execute(f"SELECT {columns} FROM sessions_meta")
        db_rows = {row["name"]: row for row in await cursor.fetchall()}

    # The exact workspace registry is established before any tmux/process probe.
    tmux_sessions = await tmux.list_sessions()
    if workspace_scope is not None:
        tmux_sessions = [s for s in tmux_sessions if s["name"] in db_rows]
    tmux_names = {s["name"] for s in tmux_sessions}
    if workspace_scope is None:
        statuses = await tmux.get_all_session_statuses() if tmux_names else {}
    else:
        status_results = await asyncio.gather(
            *(tmux.get_session_status(name) for name in tmux_names)
        )
        statuses = dict(zip(tmux_names, status_results))

    now = datetime.now(timezone.utc).isoformat()
    live_updates: list[dict[str, str | None]] = []
    for name in tmux_names:
        row = db_rows.get(name)
        if row is None and not local_compatibility:
            logger.warning("Quarantined unowned tmux session metadata: %s", name)
            continue
        if (
            row
            and row["session_uuid"]
            and row["created_at"]
            and row["project_slug"]
            and row["provider"]
            and (row["model"] or row["launch_model"])
        ):
            continue

        cwd = await tmux.get_pane_cwd(name)
        created_epoch = await tmux.get_pane_start_time(name)
        created_at = (
            datetime.fromtimestamp(created_epoch, timezone.utc).isoformat()
            if created_epoch
            else now
        )
        pid = await tmux.get_cli_pid(name, process_names=ALL_KNOWN_PROCESS_NAMES)
        command = await _process_command(pid)
        provider = (
            (row["provider"] if row and row["provider"] else None)
            or _infer_provider_from_runtime(statuses.get(name), command)
            or "claude"
        )
        model = (
            (row["model"] if row and row["model"] else None)
            or (row["launch_model"] if row and row["launch_model"] else None)
            or _infer_model_from_command(command)
        )
        project_slug = (
            (row["project_slug"] if row and row["project_slug"] else None)
            or _infer_project_from_command(command)
            or (_detect_project_from_path(cwd) if cwd else None)
        )
        live_updates.append(
            {
                "name": name,
                "workspace_id": (
                    row["workspace_id"] if row else "ws_default"
                ),
                "session_uuid": (
                    row["session_uuid"]
                    if row and row["session_uuid"]
                    else str(uuid_mod.uuid4())
                ),
                "created_at": (
                    row["created_at"] if row and row["created_at"] else created_at
                ),
                "last_active": (
                    row["last_active"] if row and row["last_active"] else now
                ),
                "project_slug": project_slug,
                "provider": provider,
                "model": model,
                "launch_model": (
                    row["launch_model"] if row and row["launch_model"] else model
                ),
            }
        )

    names_to_delete = _reconcile_names_to_delete(
        tmux_names,
        set(db_rows.keys()),
        workspace_scope,
    )
    if not live_updates and not names_to_delete:
        return 0

    changed = 0
    async with write_db() as db:
        for dead in names_to_delete:
            dead_row = db_rows.get(dead)
            if dead_row is None:
                continue
            await db.execute(
                "DELETE FROM sessions_meta WHERE name = ? AND workspace_id = ?",
                (dead, dead_row["workspace_id"]),
            )
            reconcile_scope = workspace_scope or _LOCAL_RECONCILE_SCOPE
            _reconcile_absent_cycles.pop((reconcile_scope, dead), None)
            changed += 1
        for item in live_updates:
            await db.execute(
                "INSERT OR IGNORE INTO sessions_meta "
                "(name, workspace_id, session_uuid, created_at, last_active, project_slug, provider, model, launch_model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item["name"],
                    item["workspace_id"],
                    item["session_uuid"],
                    item["created_at"],
                    item["last_active"],
                    item["project_slug"],
                    item["provider"],
                    item["model"],
                    item["launch_model"],
                ),
            )
            await db.execute(
                """UPDATE sessions_meta SET
                    session_uuid = COALESCE(NULLIF(session_uuid, ''), ?),
                    created_at = COALESCE(created_at, ?),
                    last_active = COALESCE(last_active, ?),
                    project_slug = COALESCE(NULLIF(project_slug, ''), ?),
                    provider = COALESCE(NULLIF(provider, ''), ?),
                    model = COALESCE(NULLIF(model, ''), ?),
                    launch_model = COALESCE(NULLIF(launch_model, ''), ?)
                WHERE name = ? AND workspace_id = ?""",
                (
                    item["session_uuid"],
                    item["created_at"],
                    item["last_active"],
                    item["project_slug"],
                    item["provider"],
                    item["model"],
                    item["launch_model"],
                    item["name"],
                    item["workspace_id"],
                ),
            )
            changed += 1
    if changed:
        _invalidate_sessions_cache()
    return changed


async def _recreate_tmux_session(name: str, start_command: str) -> None:
    """Recreate a tmux session with the same name and a fresh start command."""
    if await tmux.session_exists(name) and not await tmux.kill_session(name):
        raise HTTPException(status_code=500, detail="Failed to restart tmux session")
    if not await tmux.create_session(name, start_command=start_command):
        raise HTTPException(status_code=500, detail="Failed to restart session")


async def _resolve_opencode_session_id(
    *,
    db: aiosqlite.Connection,
    name: str,
    workspace_id: str,
    launch_dir: str,
    stored_session_id: str | None,
    created_at: str | None,
) -> str | None:
    if opencode_sessions.is_opencode_session_id(stored_session_id):
        return stored_session_id
    session_id = await asyncio.to_thread(
        opencode_sessions.find_session_id_for_created_at,
        launch_dir,
        created_at,
    )
    if session_id and session_id != stored_session_id:
        if await _sessions_have_workspace(db):
            await db.execute(
                "UPDATE sessions_meta SET conversation_id = ? "
                "WHERE name = ? AND workspace_id = ?",
                (session_id, name, workspace_id),
            )
        elif _allow_legacy_local_session_schema():
            await db.execute(
                "UPDATE sessions_meta SET conversation_id = ? WHERE name = ?",
                (session_id, name),
            )
    return session_id


async def _capture_new_opencode_session_id(
    *,
    db: aiosqlite.Connection,
    name: str,
    workspace_id: str,
    launch_dir: str,
    launched_at_ms: int,
) -> str | None:
    session_id = await opencode_sessions.wait_for_new_session_id(
        launch_dir,
        launched_at_ms,
    )
    if session_id:
        if await _sessions_have_workspace(db):
            await db.execute(
                "UPDATE sessions_meta SET conversation_id = ? "
                "WHERE name = ? AND workspace_id = ?",
                (session_id, name, workspace_id),
            )
        elif _allow_legacy_local_session_schema():
            await db.execute(
                "UPDATE sessions_meta SET conversation_id = ? WHERE name = ?",
                (session_id, name),
            )
    return session_id


async def _send_bootstrap_message(
    name: str,
    provider_name: str,
    message: str | None,
) -> None:
    if not message:
        return
    try:
        provider_config = get_provider(provider_name)
        for _ in range(30):
            if not await tmux.session_exists(name):
                return
            status = await tmux.get_session_status(name)
            if status and status in provider_config.process_names:
                break
            await asyncio.sleep(0.5)
        await tmux.send_keys(
            name, message, double_enter=provider_config.submit_with_double_enter
        )
        logger.info("Bootstrap sent to session %s: %r", name, message[:80])
    except Exception as exc:
        logger.warning("Bootstrap failed for session %s: %s", name, exc)


def _catalog_response() -> SessionCatalogResponse:
    providers = []
    for provider in list_provider_definitions():
        providers.append(
            SessionCatalogProvider(
                id=provider.id,
                label=provider.label,
                default_model=provider.default_model,
                launch_root=provider.launch_root,
                note=provider.note,
                models=[
                    SessionCatalogModel(
                        id=model.id,
                        label=model.label,
                        description=model.description,
                        context_window=model.context_window,
                        supports_1m=model.supports_1m,
                        recommended=model.recommended,
                        experimental=model.experimental,
                        note=model.note,
                    )
                    for model in list_catalog_models(provider.id)
                ],
                permission_presets=[
                    SessionPermissionPreset(
                        id=preset.id,
                        label=preset.label,
                        badge=preset.badge,
                        description=preset.description,
                    )
                    for preset in provider.permission_presets
                ],
            )
        )
    return SessionCatalogResponse(providers=providers)


async def _sync_sessions_impl(
    db: aiosqlite.Connection,
    *,
    allow_writes: bool,
    workspace_id: str | None = None,
) -> list[SessionInfo]:
    """Merge tmux sessions (source of truth) with DB metadata.

    Sessions in tmux but not in DB get added.
    Sessions in DB but not in tmux get removed.
    Returns sorted: pinned first, then sort_order, then name.
    """
    global _sessions_last_sync_timings
    sync_started = _time.perf_counter()
    local_compatibility = _allow_legacy_local_session_schema()
    workspace_scope = (workspace_id or "").strip() or None
    if workspace_scope is None and not local_compatibility:
        raise HTTPException(status_code=403, detail="Session not found")

    # Get DB metadata. Legacy schemas are accepted only for explicit local OSS.
    has_workspace = await _sessions_have_workspace(db)
    if not has_workspace and not local_compatibility:
        raise HTTPException(status_code=503, detail="Session registry unavailable")
    select_columns = DB_COLUMNS if has_workspace else "*"
    if workspace_scope is not None and has_workspace:
        cursor = await db.execute(
            f"SELECT {select_columns} FROM sessions_meta WHERE workspace_id = ?",
            (workspace_scope,),
        )
    else:
        cursor = await db.execute(f"SELECT {select_columns} FROM sessions_meta")
    db_rows = {row["name"]: row for row in await cursor.fetchall()}
    db_read_done = _time.perf_counter()

    # Resolve the authenticated workspace in the registry before invoking tmux.
    # The tmux service has one shared namespace, so its union listing is filtered
    # immediately against the exact scoped rows before any per-session probing.
    tmux_sessions = await tmux.list_sessions()
    if workspace_scope is not None:
        tmux_sessions = [s for s in tmux_sessions if s["name"] in db_rows]
    tmux_list_done = _time.perf_counter()
    tmux_names = {s["name"] for s in tmux_sessions}

    # Dead-row cleanup is intentionally NOT done here. This merge path runs on
    # every session-list read, and under read-lag (a pinned/stale WAL snapshot)
    # live rows can look "dead" — which is how the registry was wiped on
    # 2026-06-12. The result list is built from live tmux sessions, so dead rows
    # are invisible to the UI regardless. Deletion is owned solely by
    # reconcile_sessions_metadata(), which removes a row only after it has been
    # absent for several consecutive trustworthy cycles. (task 018a38e2)

    # Auto-register sessions without session_uuid (created from CLI, not Console)
    if allow_writes and local_compatibility and workspace_scope is None:
        for name in tmux_names:
            row = db_rows.get(name)
            if row and row["session_uuid"]:
                continue  # Already registered

            # Generate UUID for unregistered sessions
            new_uuid = str(uuid_mod.uuid4())
            now = datetime.now(timezone.utc).isoformat()

            # Do NOT infer the project from cwd here. Every session launches from
            # the shared workspace root, so cwd-inference fabricates the repo's
            # own project slug for unrelated sessions — and under read-lag it
            # makes the list flip a real project to that slug. The project is set
            # explicitly at launch; leave it NULL until then. The COALESCE on the
            # row branch below preserves any project a human already assigned.
            # (task 018a38e2 — 2026-06-12 read-path flip)
            project_slug = None

            if row:
                # Session exists in DB but no UUID — update it
                if has_workspace:
                    await db.execute(
                        "UPDATE sessions_meta SET session_uuid = ?, "
                        "project_slug = COALESCE(project_slug, ?) "
                        "WHERE name = ? AND workspace_id = 'ws_default'",
                        (new_uuid, project_slug, name),
                    )
                else:
                    await db.execute(
                        "UPDATE sessions_meta SET session_uuid = ?, "
                        "project_slug = COALESCE(project_slug, ?) WHERE name = ?",
                        (new_uuid, project_slug, name),
                    )
            else:
                # Brand new session — full insert
                if has_workspace:
                    await db.execute(
                        "INSERT OR IGNORE INTO sessions_meta "
                        "(name, workspace_id, session_uuid, created_at, last_active, project_slug) "
                        "VALUES (?, 'ws_default', ?, ?, ?, ?)",
                        (name, new_uuid, now, now, project_slug),
                    )
                else:
                    await db.execute(
                        "INSERT OR IGNORE INTO sessions_meta "
                        "(name, session_uuid, created_at, last_active, project_slug) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (name, new_uuid, now, now, project_slug),
                    )

            # Update local db_rows cache so the result loop sees the new data
            cursor = await db.execute(
                f"SELECT {select_columns} FROM sessions_meta WHERE name = ?", (name,)
            )
            updated_row = await cursor.fetchone()
            if updated_row:
                db_rows[name] = updated_row

        await db.commit()

    # Get process statuses and metrics via bulk snapshots. This keeps the
    # session-list path O(1) in subprocess calls even with hundreds of panes.
    if workspace_scope is None:
        statuses = await tmux.get_all_session_statuses()
        pane_pids = await tmux.get_all_session_pane_pids()
        process_snapshots = await tmux.get_all_process_snapshots()
    else:
        status_results = await asyncio.gather(
            *(tmux.get_session_status(name) for name in tmux_names)
        )
        statuses = dict(zip(tmux_names, status_results))
        # Avoid fleet-wide pane/process snapshots for remote callers. Those
        # diagnostics cannot currently be queried by exact workspace.
        pane_pids = {}
        process_snapshots = {}
    processes_by_parent = _index_processes_by_parent(process_snapshots)
    status_metrics_done = _time.perf_counter()

    # Build result. Under read-lag the stale snapshot can hide a persisted row;
    # never blank or re-infer a known session — reuse its last good snapshot and
    # only refresh the live fields. This keeps the project from flipping to empty
    # when the read pool is busy. (task 018a38e2 — 2026-06-12 read-path flip)
    last_known = (
        {s.name: s for s in _sessions_cache}
        if workspace_scope is None and not allow_writes and _sessions_cache
        else {}
    )
    result = []
    for ts in tmux_sessions:
        name = ts["name"]
        db_row = db_rows.get(name)

        if db_row is None and name in last_known:
            result.append(
                last_known[name].model_copy(
                    update={
                        "status": statuses.get(name),
                        "attached": ts["attached"],
                    }
                )
            )
            continue

        created_epoch = _created_epoch_from_iso(db_row["created_at"] if db_row else None)

        result.append(
            SessionInfo(
                name=name,
                workspace_id=(
                    db_row["workspace_id"]
                    if db_row and "workspace_id" in db_row.keys()
                    else None
                ),
                display_name=db_row["display_name"] if db_row else None,
                pinned=bool(db_row["pinned"]) if db_row and db_row["pinned"] else False,
                sort_order=db_row["sort_order"]
                if db_row and db_row["sort_order"]
                else 0,
                group_name=db_row["group_name"] if db_row else None,
                project_slug=db_row["project_slug"] if db_row else None,
                session_uuid=db_row["session_uuid"] if db_row else None,
                status=statuses.get(name),
                created_at=db_row["created_at"] if db_row else None,
                last_active=db_row["last_active"] if db_row else None,
                attached=ts["attached"],
                hibernated=bool(db_row["hibernated"])
                if db_row and db_row["hibernated"]
                else False,
                conversation_id=db_row["conversation_id"] if db_row else None,
                model=(
                    db_row["model"]
                    if db_row and db_row["model"]
                    else db_row["launch_model"]
                    if db_row
                    else None
                ),
                launch_model=db_row["launch_model"] if db_row else None,
                permission_preset=db_row["permission_preset"] if db_row else None,
                last_context_pct=db_row["last_context_pct"] if db_row else None,
                last_cost_usd=db_row["last_cost_usd"] if db_row else None,
                last_message_count=db_row["last_message_count"] if db_row else None,
                auto_hibernate_minutes=db_row["auto_hibernate_minutes"]
                if db_row and db_row["auto_hibernate_minutes"]
                else 30,
                working_seconds=db_row["working_seconds"]
                if db_row and db_row["working_seconds"]
                else 0,
                created_epoch=created_epoch,
                agent_managed=bool(db_row["agent_managed"])
                if db_row and db_row["agent_managed"]
                else False,
                owner_id=db_row["owner_id"]
                if db_row and "owner_id" in db_row.keys()
                else None,
                provider=db_row["provider"]
                if db_row and db_row["provider"]
                else "claude",
                # PR2 dual metrics — defensive .keys() check so legacy test DBs
                # without migration 087 still work.
                last_context_pct_real=(
                    db_row["last_context_pct_real"]
                    if db_row and "last_context_pct_real" in db_row.keys()
                    else None
                ),
                last_context_pct_scaled=(
                    db_row["last_context_pct_scaled"]
                    if db_row and "last_context_pct_scaled" in db_row.keys()
                    else None
                ),
                last_cost_conversation_usd=(
                    db_row["last_cost_conversation_usd"]
                    if db_row and "last_cost_conversation_usd" in db_row.keys()
                    else None
                ),
                last_cost_session_usd=(
                    db_row["last_cost_session_usd"]
                    if db_row and "last_cost_session_usd" in db_row.keys()
                    else None
                ),
                last_cost_session_incomplete=bool(
                    db_row["last_cost_session_incomplete"]
                    if db_row and "last_cost_session_incomplete" in db_row.keys()
                    else 0
                ),
                last_input_tokens=(
                    db_row["last_input_tokens"]
                    if db_row and "last_input_tokens" in db_row.keys()
                    else None
                ),
                last_output_tokens=(
                    db_row["last_output_tokens"]
                    if db_row and "last_output_tokens" in db_row.keys()
                    else None
                ),
                last_reasoning_tokens=(
                    db_row["last_reasoning_tokens"]
                    if db_row and "last_reasoning_tokens" in db_row.keys()
                    else None
                ),
                working_seconds_msg=(
                    db_row["working_seconds_msg"]
                    if db_row and "working_seconds_msg" in db_row.keys()
                    else None
                ),
                metrics_refreshed_at=(
                    db_row["metrics_refreshed_at"]
                    if db_row and "metrics_refreshed_at" in db_row.keys()
                    else None
                ),
                pricing_version=(
                    db_row["pricing_version"]
                    if db_row and "pricing_version" in db_row.keys()
                    else None
                ),
                # PR4 shadow cost (migration 089) — defensive .keys() check
                last_cost_conversation_equivalent_usd=(
                    db_row["last_cost_conversation_equivalent_usd"]
                    if db_row
                    and "last_cost_conversation_equivalent_usd" in db_row.keys()
                    else None
                ),
                last_cost_session_equivalent_usd=(
                    db_row["last_cost_session_equivalent_usd"]
                    if db_row
                    and "last_cost_session_equivalent_usd" in db_row.keys()
                    else None
                ),
                last_cost_equivalent_pricing_version=(
                    db_row["last_cost_equivalent_pricing_version"]
                    if db_row
                    and "last_cost_equivalent_pricing_version" in db_row.keys()
                    else None
                ),
            )
        )
    result_build_done = _time.perf_counter()

    def _session_db_scope(session_info: SessionInfo) -> tuple[str, tuple[str, ...]]:
        if has_workspace:
            scoped_workspace = session_info.workspace_id or workspace_scope
            if scoped_workspace is None and local_compatibility:
                scoped_workspace = "ws_default"
            if scoped_workspace is None:
                raise HTTPException(status_code=403, detail="Session not found")
            return (
                "name = ? AND workspace_id = ?",
                (session_info.name, scoped_workspace),
            )
        if not local_compatibility:
            raise HTTPException(status_code=503, detail="Session registry unavailable")
        return "name = ?", (session_info.name,)

    async def _persist_detected_conversation(
        session_info: SessionInfo, conversation_id: str
    ) -> None:
        where_sql, where_params = _session_db_scope(session_info)
        if has_workspace:
            await db.execute(
                "INSERT OR IGNORE INTO sessions_meta (name, workspace_id) VALUES (?, ?)",
                where_params,
            )
        else:
            await db.execute(
                "INSERT OR IGNORE INTO sessions_meta (name) VALUES (?)",
                (session_info.name,),
            )
        await db.execute(
            f"UPDATE sessions_meta SET conversation_id = ? WHERE {where_sql}",
            (conversation_id, *where_params),
        )

    if allow_writes:
        # Lightweight metrics refresh for active CLI sessions
        now_ts = datetime.now(timezone.utc)

        # Step 1: Get pane IDs for all active Claude sessions (metrics only for Claude provider)
        active_sessions = [
            s
            for s in result
            if s.status in ALL_KNOWN_PROCESS_NAMES and s.provider == "claude"
        ]
        pane_id_tasks = {s.name: tmux.get_pane_id(s.name) for s in active_sessions}
        pane_id_results = await asyncio.gather(*pane_id_tasks.values())
        pane_id_map = dict(zip(pane_id_tasks.keys(), pane_id_results))

        for session_info in active_sessions:
            # --- Detection: statusline (most reliable) → PID → timestamp ---
            pane_id = pane_id_map.get(session_info.name)
            pane_data = None

            # Method 1: Statusline per-pane file (written by statusline.sh v2.0.0+)
            if pane_id:
                pane_data = await asyncio.to_thread(
                    claude_metrics.read_pane_metrics, pane_id
                )
                if pane_data:
                    new_conv_id = pane_data.session_id
                    if new_conv_id != session_info.conversation_id:
                        session_info.conversation_id = new_conv_id
                        await _persist_detected_conversation(
                            session_info, new_conv_id
                        )

            # Method 1.5: Stale pane-metrics (session_id is valid even when idle)
            if not session_info.conversation_id and pane_id:
                stale_sid = await asyncio.to_thread(
                    claude_metrics.read_pane_session_id, pane_id
                )
                if stale_sid:
                    session_info.conversation_id = stale_sid
                    await _persist_detected_conversation(session_info, stale_sid)

            # Method 2: PID-based detection (fallback)
            if not session_info.conversation_id:
                claude_pid = await tmux.get_claude_pid(session_info.name)
                if claude_pid:
                    pid_conv_id = claude_metrics.detect_conversation_by_pid(claude_pid)
                    if pid_conv_id:
                        session_info.conversation_id = pid_conv_id
                        await _persist_detected_conversation(
                            session_info, pid_conv_id
                        )

            # Method 3: Timestamp-based detection (last resort)
            if not session_info.conversation_id:
                pane_start = await tmux.get_pane_start_time(session_info.name)
                if pane_start:
                    conv_id = claude_metrics.detect_conversation_for_session(
                        pane_start,
                        cwd=resolve_project_path(session_info.project_slug),
                    )
                    if conv_id:
                        session_info.conversation_id = conv_id
                        await _persist_detected_conversation(session_info, conv_id)
                if not session_info.conversation_id:
                    continue

            conv_cwd = await asyncio.to_thread(
                _resolve_conversation_cwd,
                session_info.conversation_id,
                session_info.project_slug,
            )

            # --- Metrics refresh ---
            # If we have fresh statusline data, use it directly (no JSONL parse needed for context_pct)
            # PR3: write the real ratio to last_context_pct_real (no /84 fudge).
            if pane_data and pane_data.session_id == session_info.conversation_id:
                real_pct = min(round(pane_data.used_pct, 1), 100.0)
                session_info.last_context_pct = real_pct
                session_info.last_context_pct_real = real_pct
                where_sql, where_params = _session_db_scope(session_info)
                cursor_meta = await db.execute(
                    f"SELECT last_metrics_at FROM sessions_meta WHERE {where_sql}",
                    where_params,
                )
                meta_row = await cursor_meta.fetchone()
                should_parse = True
                if meta_row and meta_row["last_metrics_at"]:
                    try:
                        last_refresh = datetime.fromisoformat(
                            meta_row["last_metrics_at"].replace("Z", "+00:00")
                        )
                        if last_refresh.tzinfo is None:
                            last_refresh = last_refresh.replace(tzinfo=timezone.utc)
                        if (now_ts - last_refresh).total_seconds() < 10:
                            should_parse = False
                    except ValueError:
                        pass

                if should_parse:
                    metrics = None
                    if conv_cwd:
                        metrics = await asyncio.to_thread(
                            claude_metrics.find_conversation_by_id,
                            session_info.conversation_id,
                            conv_cwd,
                        )
                    if metrics:
                        session_info.last_cost_usd = metrics.cost_usd
                        session_info.last_message_count = metrics.message_count
                        if metrics.model:
                            session_info.model = metrics.model
                    await db.execute(
                        """UPDATE sessions_meta SET
                            model = ?, last_context_pct_real = ?, last_cost_usd = ?,
                            last_message_count = ?, last_metrics_at = datetime('now')
                        WHERE {where_sql}""".format(where_sql=where_sql),
                        (
                            session_info.model,
                            real_pct,
                            session_info.last_cost_usd,
                            session_info.last_message_count,
                            *where_params,
                        ),
                    )
                else:
                    await db.execute(
                        f"UPDATE sessions_meta SET last_context_pct_real = ? "
                        f"WHERE {where_sql}",
                        (real_pct, *where_params),
                    )
            else:
                where_sql, where_params = _session_db_scope(session_info)
                cursor_meta = await db.execute(
                    f"SELECT last_metrics_at FROM sessions_meta WHERE {where_sql}",
                    where_params,
                )
                meta_row = await cursor_meta.fetchone()
                last_refresh_epoch = 0.0
                if meta_row and meta_row["last_metrics_at"]:
                    try:
                        last_refresh = datetime.fromisoformat(
                            meta_row["last_metrics_at"].replace("Z", "+00:00")
                        )
                        if last_refresh.tzinfo is None:
                            last_refresh = last_refresh.replace(tzinfo=timezone.utc)
                        last_refresh_epoch = last_refresh.timestamp()
                        if (now_ts - last_refresh).total_seconds() < 5:
                            continue
                    except ValueError:
                        pass
                if not conv_cwd:
                    continue
                if last_refresh_epoch:
                    jsonl_mtime = await asyncio.to_thread(
                        claude_metrics.get_jsonl_mtime,
                        session_info.conversation_id,
                        conv_cwd,
                    )
                    if jsonl_mtime and jsonl_mtime <= last_refresh_epoch:
                        continue

                context_pct = await asyncio.to_thread(
                    claude_metrics.get_last_context_pct,
                    session_info.conversation_id,
                    conv_cwd,
                )
                if context_pct is not None:
                    session_info.last_context_pct = context_pct
                    session_info.last_context_pct_real = context_pct

                metrics = await asyncio.to_thread(
                    claude_metrics.find_conversation_by_id,
                    session_info.conversation_id,
                    conv_cwd,
                )
                if metrics:
                    effective_real = (
                        context_pct if context_pct is not None else metrics.context_pct
                    )
                    if context_pct is None:
                        session_info.last_context_pct = metrics.context_pct
                        session_info.last_context_pct_real = metrics.context_pct
                    session_info.last_cost_usd = metrics.cost_usd
                    session_info.last_message_count = metrics.message_count
                    session_info.model = metrics.model
                    # PR3: write context_pct to _real; legacy column untouched.
                    await db.execute(
                        """UPDATE sessions_meta SET
                            model = ?, last_context_pct_real = ?, last_cost_usd = ?,
                            last_message_count = ?, last_metrics_at = datetime('now')
                        WHERE {where_sql}""".format(where_sql=where_sql),
                        (
                            metrics.model,
                            effective_real,
                            metrics.cost_usd,
                            metrics.message_count,
                            *where_params,
                        ),
                    )

        await db.commit()
    write_metrics_done = _time.perf_counter()

    # Apply event-driven activity_state from DB to ALL sessions, regardless of
    # current pane status. PR1.5 hotfix (2026-04-27): the previous version
    # applied this only inside the cli_sessions loop, so when a tmux pane was
    # running a Bash tool (status="bash") the session was excluded and
    # activity_state stayed None despite a fresh "working" event in the DB.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    _now_utc = _dt.now(_tz.utc)
    _event_cutoff = _now_utc - _td(seconds=_ACTIVITY_EVENT_TTL_SECS)

    def _fresh_event_state(db_row) -> str | None:
        if db_row is None:
            return None
        event_state = db_row["activity_state"]
        event_ts_iso = db_row["activity_state_updated_at"]
        if not (event_state and event_ts_iso):
            return None
        try:
            event_ts = _dt.fromisoformat(event_ts_iso.replace("Z", "+00:00"))
            if event_ts.tzinfo is None:
                event_ts = event_ts.replace(tzinfo=_tz.utc)
        except (ValueError, AttributeError):
            return None
        return event_state if event_ts > _event_cutoff else None

    for s in result:
        fresh = _fresh_event_state(db_rows.get(s.name))
        if fresh:
            s.activity_state = fresh
    activity_merge_done = _time.perf_counter()

    # No pane scraping on the global session-list request path. Activity state
    # is event-driven; process metrics are derived from bulk tmux+ps snapshots.
    cli_sessions = [s for s in result if s.status in ALL_KNOWN_PROCESS_NAMES]
    process_metrics_count = 0
    for s in cli_sessions:
        if not s.activity_state and s.provider != "claude":
            s.activity_state = "active"

        process = _session_process_snapshot(s, pane_pids, processes_by_parent)
        if process:
            process_metrics_count += 1
            s.cpu_pct = round(process.cpu_pct, 1)
            s.ram_mb = round(process.rss_kb / 1024, 1)
    process_mapping_done = _time.perf_counter()

    # Sort: pinned first, then sort_order, then name
    result.sort(key=lambda s: (not s.pinned, s.sort_order, s.name))
    sync_done = _time.perf_counter()
    _sessions_last_sync_timings = {
        "workspace_id": workspace_scope or "ws_default",
        "allow_writes": allow_writes,
        "session_count": len(result),
        "db_read_ms": (db_read_done - sync_started) * 1000,
        "tmux_list_ms": (tmux_list_done - db_read_done) * 1000,
        "status_metrics_ms": (status_metrics_done - tmux_list_done) * 1000,
        "result_build_ms": (result_build_done - status_metrics_done) * 1000,
        "write_metrics_ms": (write_metrics_done - result_build_done) * 1000,
        "activity_merge_ms": (activity_merge_done - write_metrics_done) * 1000,
        "process_mapping_ms": (process_mapping_done - activity_merge_done) * 1000,
        "pane_fallback_ms": 0.0,
        "pane_scrape_count": 0,
        "process_metrics_count": process_metrics_count,
        "total_ms": (sync_done - sync_started) * 1000,
    }
    return result


async def _sync_sessions(
    db: aiosqlite.Connection,
    workspace_id: str | None = None,
) -> list[SessionInfo]:
    return await _sync_sessions_impl(
        db, allow_writes=True, workspace_id=workspace_id
    )


async def _sync_sessions_read_only(
    db: aiosqlite.Connection,
    workspace_id: str | None = None,
) -> list[SessionInfo]:
    return await _sync_sessions_impl(
        db, allow_writes=False, workspace_id=workspace_id
    )


@router.get("", response_model=list[SessionInfo])
async def list_sessions(
    request: Request,
    agent_managed: bool | None = None,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """List tmux sessions. Operators see only their own; admins see all."""
    global _sessions_cache_pending_patch_state
    started = _time.perf_counter()
    workspace_id = _workspace_for_user(current_user)
    if _allow_legacy_local_session_schema():
        sessions = await _get_sessions_cached(db)
    else:
        sessions = await _get_sessions_cached(db, workspace_id=workspace_id)
    sessions = [
        session
        for session in sessions
        if session.workspace_id == workspace_id
        or (_allow_legacy_local_session_schema() and session.workspace_id is None)
    ]
    unfiltered_count = len(sessions)
    cache_state = _sessions_cache_pending_patch_state or _sessions_cache_last_state
    sync_timings = _sessions_last_sync_timings
    if sync_timings and sync_timings.get("workspace_id") != workspace_id:
        sync_timings = None

    # RBAC: operator/viewer humans are owner-scoped. Only explicit system agent
    # identities retain global visibility on this read endpoint.
    if not _can_view_all_sessions(current_user) and current_user.system_role in (
        "operator",
        "viewer",
    ):
        sessions = [s for s in sessions if s.owner_id == current_user.user_id]
    elif current_user.system_role == "team_admin":
        # team_admin sees sessions owned by any member of their teams
        async with db.execute(
            """SELECT DISTINCT tm2.user_id FROM team_members tm1
               JOIN team_members tm2 ON tm1.team_id = tm2.team_id
               JOIN teams t ON t.id = tm1.team_id
               JOIN users u1 ON u1.id = tm1.user_id
               JOIN users u2 ON u2.id = tm2.user_id
               WHERE tm1.user_id = ? AND t.workspace_id = ?
                 AND u1.workspace_id = ? AND u2.workspace_id = ?""",
            [current_user.user_id, workspace_id, workspace_id, workspace_id],
        ) as cursor:
            member_rows = await cursor.fetchall()
        team_user_ids = {r[0] for r in member_rows}
        sessions = [s for s in sessions if s.owner_id in team_user_ids]
    # admin / super_admin / agent: no filter

    if agent_managed is not None:
        sessions = [
            s
            for s in sessions
            if bool(getattr(s, "agent_managed", False)) == agent_managed
        ]
    duration_ms = (_time.perf_counter() - started) * 1000
    metadata: dict[str, Any] = {
        "cache_state": cache_state,
        "unfiltered_count": unfiltered_count,
        "returned_count": len(sessions),
        "agent_managed_filter": agent_managed,
    }
    if sync_timings:
        metadata["last_sync_total_ms"] = round(float(sync_timings["total_ms"]), 3)
        metadata["last_sync_session_count"] = sync_timings["session_count"]
    _record_sessions_control_event(
        request,
        workspace_id=workspace_id,
        kind="list",
        duration_ms=duration_ms,
        metadata=metadata,
    )
    if cache_state == "miss_wait" and sync_timings:
        _record_sessions_control_event(
            request,
            workspace_id=workspace_id,
            kind="sync",
            duration_ms=float(sync_timings["total_ms"]),
            metadata={
                key: round(value, 3) if isinstance(value, float) else value
                for key, value in sync_timings.items()
            },
        )
    if _sessions_cache_pending_patch_state == "state_patched":
        _sessions_cache_pending_patch_state = None
    return sessions


@router.get("/catalog", response_model=SessionCatalogResponse)
async def get_session_catalog(
    _user: UserInfo = Depends(get_current_user_or_agent),
):
    return _catalog_response()


@router.post("", response_model=SessionInfo, status_code=201)
async def create_session(
    body: SessionCreate,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Create a new tmux session. Sets owner_id from the authenticated user.

    Sessions always launch from the shared MarvisX workspace so providers pick up
    the same instruction files, hooks, skills, and MCP config. Project
    selection remains metadata plus extra access directories where supported.
    """
    workspace_id = _workspace_for_user(current_user)
    await _require_project_workspace(db, current_user, body.project_slug)
    try:
        launch_spec = build_session_start_spec(
            body.provider,
            body.project_slug,
            body.model,
            body.permission_preset,
            session_name=body.name,
            theme_mode=body.theme_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    provider_config = launch_spec.provider_config
    start_command = launch_spec.start_command
    provider_name = launch_spec.provider
    selected_model = launch_spec.cli_model
    selected_model_id = launch_spec.model_id
    permission_preset = launch_spec.permission_preset
    bootstrap_message = _project_bootstrap_message(body.project_slug)
    launched_at_ms = int(_time.time() * 1000)

    scoped_row = await _fetch_session_meta_row(db, body.name, workspace_id)
    if (
        await _sessions_have_workspace(db)
        and not _allow_legacy_local_session_schema()
    ):
        foreign_row = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta WHERE name = ? "
                "AND (workspace_id IS NULL OR workspace_id != ?)",
                (body.name, workspace_id),
            )
        ).fetchone()
        if foreign_row is not None:
            raise HTTPException(status_code=409, detail="Session already exists")

    existing_server = await tmux.resolve_session_server(body.name)
    if existing_server is not None:
        row = scoped_row
        if row is None and not _allow_legacy_local_session_schema():
            raise HTTPException(status_code=409, detail="Session already exists")
        if existing_server != "marvisx" or not _is_claimable_marvisx_orphan(
            row, current_user
        ):
            raise HTTPException(status_code=409, detail="Session already exists")

        session_uuid, created_at, now = await _persist_session_create_metadata(
            db,
            name=body.name,
            current_user=current_user,
            project_slug=body.project_slug,
            provider_name=provider_name,
            selected_model=selected_model,
            selected_model_id=selected_model_id,
            permission_preset=permission_preset,
            theme_mode=body.theme_mode,
            bootstrap_message=bootstrap_message,
        )
        await db.commit()
        _invalidate_sessions_cache()
        asyncio.create_task(
            _broadcast_session_event("created", workspace_id=workspace_id)
        )

        logger.warning(
            "Recovered orphan tmux session metadata: %s "
            "(uuid=%s, owner=%s, project=%s, provider=%s)",
            body.name,
            session_uuid,
            current_user.user_id,
            body.project_slug,
            provider_name,
        )
        return SessionInfo(
            name=body.name,
            workspace_id=workspace_id,
            session_uuid=session_uuid,
            created_at=created_at,
            last_active=now,
            attached=False,
            owner_id=current_user.user_id or None,
            project_slug=body.project_slug,
            provider=provider_name,
            model=selected_model,
            launch_model=selected_model_id,
            permission_preset=permission_preset,
            conversation_id=_row_value(row, "conversation_id"),
        )

    if not await is_binary_available(provider_config):
        raise HTTPException(
            status_code=422,
            detail=f"CLI '{provider_config.binary}' not found on server",
        )

    tmux_user_env = await _tmux_user_env_for_session(db, current_user)
    if tmux_user_env is None and not settings.multi_tenant_enabled:
        success = await tmux.create_session(
            body.name,
            start_command=start_command,
        )
    else:
        success = await tmux.create_session(
            body.name,
            start_command=start_command,
            tenant_slug=(
                settings.deploy_mode if settings.multi_tenant_enabled else None
            ),
            user_env=tmux_user_env,
        )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to create session")

    session_uuid, created_at, now = await _persist_session_create_metadata(
        db,
        name=body.name,
        current_user=current_user,
        project_slug=body.project_slug,
        provider_name=provider_name,
        selected_model=selected_model,
        selected_model_id=selected_model_id,
        permission_preset=permission_preset,
        theme_mode=body.theme_mode,
        bootstrap_message=bootstrap_message,
    )

    opencode_session_id = None
    if provider_name == "opencode":
        opencode_session_id = await _capture_new_opencode_session_id(
            db=db,
            name=body.name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            launched_at_ms=launched_at_ms,
        )
    await db.commit()
    _invalidate_sessions_cache()
    asyncio.create_task(
        _broadcast_session_event("created", workspace_id=workspace_id)
    )

    if bootstrap_message:
        asyncio.create_task(
            _send_bootstrap_message(body.name, provider_name, bootstrap_message)
        )

    logger.info(
        "Session created: %s (uuid=%s, owner=%s, project=%s, provider=%s)",
        body.name,
        session_uuid,
        current_user.user_id,
        body.project_slug,
        provider_name,
    )
    return SessionInfo(
        name=body.name,
        workspace_id=workspace_id,
        session_uuid=session_uuid,
        created_at=created_at,
        last_active=now,
        attached=False,
        owner_id=current_user.user_id or None,
        project_slug=body.project_slug,
        provider=provider_name,
        model=selected_model,
        launch_model=selected_model_id,
        permission_preset=permission_preset,
        conversation_id=opencode_session_id,
    )


@router.delete("/{name}", status_code=204)
@_session_operation("delete")
async def delete_session(
    name: str,
    _user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Kill a tmux session."""
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    workspace_id = _workspace_for_user(_user)
    await _require_session_row(db, _user, name=name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="Session not found")

    success = await tmux.kill_session(name)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to kill session")

    if _allow_legacy_local_session_schema():
        await db.execute("DELETE FROM sessions_meta WHERE name = ?", (name,))
    else:
        await db.execute(
            "DELETE FROM sessions_meta WHERE name = ? AND workspace_id = ?",
            (name, workspace_id),
        )
    await db.commit()

    _invalidate_sessions_cache()
    asyncio.create_task(
        _broadcast_session_event("destroyed", workspace_id=workspace_id)
    )

    logger.info("Session deleted: %s", name)


@router.patch("/{name}", response_model=SessionInfo)
async def update_session(
    name: str,
    body: SessionUpdate,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Update session metadata: rename, display_name, pin, group, project_slug, agent_managed."""
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    workspace_id = _workspace_for_user(current_user)
    session_row = await _require_session_row(db, current_user, name=name)
    if (
        _allow_legacy_local_session_schema()
        and "workspace_id" in session_row.keys()
        and session_row["workspace_id"] != workspace_id
    ):
        await db.execute(
            "UPDATE sessions_meta SET workspace_id = ? WHERE name = ?",
            (workspace_id, name),
        )
    await _require_project_workspace(db, current_user, body.project_slug)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="Session not found")

    # Handle rename if requested
    effective_name = name
    if body.new_name and body.new_name != name:
        target_row = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta WHERE name = ?", (body.new_name,)
            )
        ).fetchone()
        if target_row is not None:
            raise HTTPException(
                status_code=409, detail="Target session name already exists"
            )
        if await tmux.session_exists(body.new_name):
            raise HTTPException(
                status_code=409, detail="Target session name already exists"
            )
        success = await tmux.rename_session(name, body.new_name)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to rename session")
        if _allow_legacy_local_session_schema():
            await db.execute(
                "UPDATE session_costs SET workspace_id = ? "
                "WHERE workspace_id IS NULL AND session_name = ?",
                (workspace_id, name),
            )
        await db.execute(
            "UPDATE sessions_meta SET name = ? WHERE name = ? AND workspace_id = ?",
            (body.new_name, name, workspace_id),
        )
        # Migration 185 uses ON UPDATE CASCADE for this exact composite parent.
        # The explicit update is a safe fallback when a local SQLite connection
        # has foreign-key enforcement disabled.
        await db.execute(
            "UPDATE session_conversations SET session_name = ? "
            "WHERE workspace_id = ? AND session_name = ?",
            (body.new_name, workspace_id, name),
        )
        await db.execute(
            "UPDATE session_costs SET session_name = ? "
            "WHERE workspace_id = ? AND session_name = ?",
            (body.new_name, workspace_id, name),
        )
        # Sync display_name if it was auto-default (== old name). SessionCardV2
        # sidebar uses `display_name || name` as the primary label, so a stale
        # display_name shadows the rename. Preserve custom user labels (when
        # display_name ≠ old name) by skipping the sync. Plan 2026-05-22.
        await db.execute(
            "UPDATE sessions_meta SET display_name = ? "
            "WHERE name = ? AND workspace_id = ? AND display_name = ?",
            (body.new_name, body.new_name, workspace_id, name),
        )
        await db.commit()
        effective_name = body.new_name
        _invalidate_sessions_cache()
        # Build compact delta for WS broadcast (~9 fields, <2KB) — avoids full
        # SessionInfo (~50 fields) + privacy leak cross-tab (no conversation_ids).
        # Plan 2026-05-21: closes post-rename stale sidebar by giving clients
        # an optimistic patch payload instead of forcing a refetch round-trip.
        _delta_cursor = await db.execute(
            "SELECT display_name, provider, model, project_slug "
            "FROM sessions_meta WHERE name = ? AND workspace_id = ?",
            (effective_name, workspace_id),
        )
        _delta_row = await _delta_cursor.fetchone()
        _now_iso = datetime.now(timezone.utc).isoformat()
        _rename_delta = {
            "name": effective_name,
            "prev_name": name,
            "display_name": _delta_row["display_name"] if _delta_row else None,
            "provider": _delta_row["provider"] if _delta_row else None,
            "model": _delta_row["model"] if _delta_row else None,
            "project_slug": _delta_row["project_slug"] if _delta_row else None,
            "updated_at": _now_iso,
        }
        asyncio.create_task(
            _broadcast_session_event(
                "renamed",
                workspace_id=workspace_id,
                old_name=name,
                new_name=effective_name,
                session_info=_rename_delta,
            )
        )

        # Provider-agnostic conversation rename via /rename slash command.
        # Verified 2026-04-24 via upstream GitHub issues:
        #   - Claude Code: native /rename
        #   - OpenCode: /rename exists (anomalyco/opencode#9398)
        #   - Codex CLI: /rename (openai/codex#15533 — "already supported in the CLI")
        # Gemini CLI has no /rename; skipped. Rename is still reflected at
        # tmux + Marvis DB level, which is enough for the Console UI.
        _prov_cursor = await db.execute(
            "SELECT provider FROM sessions_meta WHERE name = ? AND workspace_id = ?",
            (effective_name, workspace_id),
        )
        _prov_row = await _prov_cursor.fetchone()
        _session_provider = (
            _prov_row["provider"] if _prov_row and _prov_row["provider"] else "claude"
        )
        if _session_provider in ("claude", "opencode", "codex"):
            await tmux.send_keys_raw(effective_name, "C-u")
            await asyncio.sleep(0.2)
            await tmux.send_keys_raw(effective_name, f"/rename {body.new_name}")
            await asyncio.sleep(0.3)
            await tmux.send_keys_raw(effective_name, "Escape")
            await asyncio.sleep(0.2)
            await tmux.send_keys_raw(effective_name, "Enter")
            logger.info(
                "Sent /rename %s to %s TUI in session %s",
                body.new_name,
                _session_provider,
                effective_name,
            )
            # /rename slash often forks the conversation into a new JSONL/UUID
            # (Claude Code creates a new file under ~/.claude/projects/...).
            # If we keep the stale conversation_id, metrics providers open a
            # ghost file and every metric (ctx %, cost, tokens, working_seconds)
            # turns NULL until manual relink. Reset the linked conversation_id
            # and the cached metrics — list_sessions() refresh logic (statusline
            # pane file → PID detect → recent-jsonl-by-cwd) will repopulate
            # them automatically on the next poll.
            await db.execute(
                "UPDATE sessions_meta SET "
                "conversation_id = NULL, "
                "last_context_pct_real = NULL, "
                "last_context_pct_scaled = NULL, "
                "last_cost_conversation_usd = NULL, "
                "last_cost_session_usd = NULL, "
                "last_input_tokens = NULL, "
                "last_output_tokens = NULL, "
                "last_reasoning_tokens = NULL, "
                "last_metrics_at = NULL, "
                "metrics_refreshed_at = NULL "
                "WHERE name = ? AND workspace_id = ?",
                (effective_name, workspace_id),
            )

    # Ensure DB row exists
    await db.execute(
        "INSERT OR IGNORE INTO sessions_meta (name, workspace_id) VALUES (?, ?)",
        (effective_name, workspace_id),
    )

    # Update metadata fields (use model_fields_set to distinguish "not sent" from "sent as null")
    updates = []
    params = []
    if "display_name" in body.model_fields_set:
        updates.append("display_name = ?")
        params.append(body.display_name or None)
    if "pinned" in body.model_fields_set:
        updates.append("pinned = ?")
        params.append(1 if body.pinned else 0)
    if "group_name" in body.model_fields_set:
        updates.append("group_name = ?")
        params.append(body.group_name or None)
    if "project_slug" in body.model_fields_set:
        updates.append("project_slug = ?")
        params.append(body.project_slug or None)
        updates.append("bootstrap_message = ?")
        params.append(_project_bootstrap_message(body.project_slug or None))
    if "agent_managed" in body.model_fields_set:
        if current_user.system_role not in ("operator", "admin", "super_admin"):
            raise HTTPException(403, "Insufficient permissions to change agent_managed")
        updates.append("agent_managed = ?")
        params.append(1 if body.agent_managed else 0)
    if "owner_id" in body.model_fields_set:
        if current_user.system_role not in ("admin", "super_admin"):
            raise HTTPException(403, "Insufficient permissions to change owner_id")
        if body.owner_id:
            owner = await (
                await db.execute(
                    "SELECT 1 FROM users WHERE id = ? AND workspace_id = ? "
                    "AND deleted_at IS NULL",
                    (body.owner_id, workspace_id),
                )
            ).fetchone()
            if owner is None:
                raise HTTPException(status_code=404, detail="User not found")
        updates.append("owner_id = ?")
        params.append(body.owner_id or None)

    now = datetime.now(timezone.utc).isoformat()
    updates.append("last_active = ?")
    params.append(now)

    if updates:
        params.extend((effective_name, workspace_id))
        await db.execute(
            f"UPDATE sessions_meta SET {', '.join(updates)} "
            "WHERE name = ? AND workspace_id = ?",
            params,
        )
        await db.commit()
        _invalidate_sessions_cache()
        asyncio.create_task(
            _broadcast_session_event("updated", workspace_id=workspace_id)
        )

    # Return updated info
    cursor = await db.execute(
        f"SELECT {DB_COLUMNS} FROM sessions_meta WHERE name = ? AND workspace_id = ?",
        (effective_name, workspace_id),
    )
    row = await cursor.fetchone()
    session_provider = row["provider"] if row and row["provider"] else "claude"
    status = await tmux.get_session_status(effective_name)
    pane_text = (
        await tmux.capture_pane(effective_name)
        if status in ALL_KNOWN_PROCESS_NAMES
        else None
    )
    activity = tmux.detect_activity_state(pane_text, status, provider=session_provider)

    return SessionInfo(
        name=effective_name,
        workspace_id=workspace_id,
        display_name=row["display_name"] if row else None,
        pinned=bool(row["pinned"]) if row and row["pinned"] else False,
        sort_order=row["sort_order"] if row and row["sort_order"] else 0,
        group_name=row["group_name"] if row else None,
        project_slug=row["project_slug"] if row else None,
        session_uuid=row["session_uuid"] if row else None,
        status=status,
        created_at=row["created_at"] if row else None,
        last_active=row["last_active"] if row else now,
        attached=False,
        model=row["model"]
        if row and row["model"]
        else row["launch_model"]
        if row
        else None,
        launch_model=row["launch_model"] if row else None,
        permission_preset=row["permission_preset"] if row else None,
        activity_state=activity,
        owner_id=row["owner_id"] if row and "owner_id" in row.keys() else None,
        provider=session_provider,
    )


@router.put("/reorder", response_model=list[SessionInfo])
async def reorder_sessions(
    body: SessionReorder,
    _user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Update sort_order for sessions based on position in list."""
    workspace_id = _workspace_for_user(_user)
    for name in body.order:
        await _require_session_row(db, _user, name=name)
    for i, name in enumerate(body.order):
        await db.execute(
            "UPDATE sessions_meta SET sort_order = ? "
            "WHERE name = ? AND workspace_id = ?",
            (i, name, workspace_id),
        )
    await db.commit()
    if _allow_legacy_local_session_schema():
        sessions = await _sync_sessions_read_only(db)
    else:
        sessions = await _sync_sessions_read_only(db, workspace_id=workspace_id)
    return [session for session in sessions if session.workspace_id == workspace_id]


# --- Session state event endpoint (PR1, plan 2026-04-26) ---
#
# In-memory rate-limit token bucket. Per-worker (uvicorn runs N workers; with
# N=4 the effective cap is 4 * cap), single-tenant Marvis context makes this
# acceptable. Documented in plan §H15. Switch to SQLite/Redis-backed if
# multi-tenant ever becomes scope.
#
# Two priority buckets (julik R5, plan M4):
#   - terminal events (Stop, StopFailure, SessionEnd, *.idle, *.error,
#     permission.updated): NO LIMIT — these are state-defining, dropping
#     leaves the session stuck in "working" until the 60s fallback gate.
#   - working events (PreToolUse, *.active): 10/s cap. Storms during
#     parallel tool batches are deduped by the LWW UPDATE anyway.

_TERMINAL_EVENTS = frozenset(
    {
        "Stop",
        "StopFailure",
        "SessionEnd",
        "session.status:idle",
        "session.status:error",
        "session.error",
        "session.idle",
        "session.deleted",
        "permission.updated",
    }
)

_RATE_LIMIT_CAP = 10  # working events per second per session
_RATE_LIMIT_WINDOW = 1.0  # seconds
_rate_buckets: dict[str, list[float]] = {}


def _check_rate_limit(session_name: str, event: str) -> bool:
    """Return True if event is allowed, False if dropped by rate limit.

    Terminal events bypass. Per-session sliding window for working events.
    """
    if event in _TERMINAL_EVENTS:
        return True
    now = _time.monotonic()
    window = _rate_buckets.setdefault(session_name, [])
    cutoff = now - _RATE_LIMIT_WINDOW
    # Trim old timestamps in place.
    window[:] = [ts for ts in window if ts > cutoff]
    if len(window) >= _RATE_LIMIT_CAP:
        return False
    window.append(now)
    return True


@router.post("/{identifier}/state", status_code=204)
async def update_session_state(
    identifier: str,
    body: SessionStateUpdate,
    request: Request,
    user: UserInfo = Depends(get_current_user_or_agent),
    _scope: UserInfo = Depends(require_scope("write")),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Record a session state event from a provider hook/plugin.

    Single endpoint accepts either tmux session name or conversation_id (UUID
    for Claude, `ses_*` for OpenCode). Resolved server-side via
    `session_state.resolve_session_name`. The {identifier} path param is
    validated/looked-up before any DB write.

    Returns 204 on success, 422 on invalid payload, 403 on missing scope, 404
    if the session can't be resolved. Rate-limited working events are silently
    dropped at 204 (no error to hook script — it's fire-and-forget).
    """
    # Operator role check (humans + agent tokens). require_scope("write")
    # already enforced by Depends.
    if not (
        user.system_role in ("operator", "admin", "super_admin")
        or user.user_type == "agent"
    ):
        raise HTTPException(status_code=403, detail="operator role required")

    # Validate / resolve identifier. Reject early with 400 (not 500 from
    # uncaught ValueError — security P1-1).
    try:
        # Tmux name validator is strict regex; conversation_ids contain `-`
        # and `_` which the regex allows, so it accepts both shapes.
        tmux.validate_session_name(identifier)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    workspace_id = _workspace_for_user(user)
    if _allow_legacy_local_session_schema():
        session_name = await session_state_svc.resolve_session_name(db, identifier)
        if session_name is None:
            return
    elif await _sessions_have_workspace(db):
        resolved_rows = await (
            await db.execute(
                "SELECT name FROM sessions_meta WHERE workspace_id = ? "
                "AND (name = ? OR conversation_id = ?) LIMIT 2",
                (workspace_id, identifier, identifier),
            )
        ).fetchall()
        if len(resolved_rows) != 1:
            # Don't 404 noisily — hooks can race against tmux session deletion.
            # 204 silent so hook script doesn't retry-storm.
            return
        session_name = resolved_rows[0]["name"]
    else:
        raise HTTPException(status_code=503, detail="Session registry unavailable")

    # Parse + bound-check client timestamp (rejects future spam + stale).
    client_ts = session_state_svc.parse_client_ts(body.ts)
    if client_ts is None:
        return  # silent drop — already logged in parse_client_ts

    # Rate-limit (priority bucket: terminal events bypass).
    rate_limit_key = (
        session_name
        if _allow_legacy_local_session_schema()
        else f"{workspace_id}:{session_name}"
    )
    if not _check_rate_limit(rate_limit_key, body.event):
        return  # silent drop, no 429 — hook is fire-and-forget

    if _allow_legacy_local_session_schema():
        state = await session_state_svc.record_state_event(
            db, session_name, body.provider, body.event, client_ts
        )
    else:
        state = session_state_svc.map_event_to_state(body.provider, body.event)
        if state is not None:
            ts_iso = client_ts.isoformat()
            cursor = await db.execute(
                """UPDATE sessions_meta
                   SET activity_state = ?, activity_state_updated_at = ?
                   WHERE name = ? AND workspace_id = ?
                     AND (activity_state_updated_at IS NULL
                          OR activity_state_updated_at < ?)""",
                (state, ts_iso, session_name, workspace_id, ts_iso),
            )
            if cursor.rowcount == 0:
                state = None
    await db.commit()
    if state is None:
        # Event ignored or LWW race lost — no broadcast spurious.
        return

    # State-only updates patch the unfiltered full-list cache in place instead
    # of forcing the next /sessions caller into a cold tmux+DB sync. If the
    # cache exists but does not contain this resolved session, invalidate once
    # so the frontend fallback full refresh can converge.
    patched_cache = _patch_sessions_cache_activity_state(
        session_name, state, workspace_id=workspace_id
    )
    if not patched_cache and _sessions_cache is not None:
        _invalidate_sessions_cache()
    # Broadcast inline state payload (M1, julik R1) so frontend can apply
    # delta optimistically without round-tripping back to GET /sessions.
    asyncio.create_task(
        _broadcast_session_event(
            "updated",
            workspace_id=workspace_id,
            session_name=session_name,
            state=state,
        )
    )


@router.post("/{name}/resurrect", response_model=SessionInfo)
async def resurrect_session(
    name: str,
    _user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Recreate a dead tmux session, preserving DB metadata."""
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    workspace_id = _workspace_for_user(_user)
    row = await _require_session_row(db, _user, name=name)
    if await tmux.session_exists(name):
        raise HTTPException(status_code=409, detail="Session is still alive")

    # Read provider from the already workspace-scoped row.
    launch_spec = build_session_start_spec(
        row["provider"] if row else None,
        row["project_slug"] if row else None,
        row["launch_model"] if row else None,
        row["permission_preset"] if row else None,
        session_name=name,
        theme_mode=row["theme_mode"] if row else None,
    )
    session_provider = launch_spec.provider
    if session_provider == "opencode":
        opencode_session_id = await _resolve_opencode_session_id(
            db=db,
            name=name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            stored_session_id=row["conversation_id"] if row else None,
            created_at=row["created_at"] if row else None,
        )
        launch_spec = build_session_start_spec(
            session_provider,
            row["project_slug"] if row else None,
            row["launch_model"] if row else None,
            row["permission_preset"] if row else None,
            resume_session_id=opencode_session_id,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        )
    start_cmd = launch_spec.start_command
    launched_at_ms = int(_time.time() * 1000)

    success = await tmux.create_session(name, start_command=start_cmd)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to resurrect session")

    if session_provider == "opencode" and not row["conversation_id"]:
        await _capture_new_opencode_session_id(
            db=db,
            name=name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            launched_at_ms=launched_at_ms,
        )

    # Update last_active
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE sessions_meta SET last_active = ? "
        "WHERE name = ? AND workspace_id = ?",
        (now, name, workspace_id),
    )
    await db.commit()

    logger.info("Session resurrected: %s (provider=%s)", name, session_provider)
    return SessionInfo(
        name=name,
        workspace_id=workspace_id,
        display_name=row["display_name"] if row else None,
        pinned=bool(row["pinned"]) if row and row["pinned"] else False,
        sort_order=row["sort_order"] if row and row["sort_order"] else 0,
        group_name=row["group_name"] if row else None,
        project_slug=row["project_slug"] if row else None,
        session_uuid=row["session_uuid"] if row else None,
        created_at=row["created_at"] if row else now,
        last_active=now,
        attached=False,
        provider=session_provider,
        model=row["model"]
        if row and row["model"]
        else row["launch_model"]
        if row
        else None,
        launch_model=row["launch_model"] if row else None,
        permission_preset=row["permission_preset"] if row else None,
    )


@router.get("/{name}/metrics", response_model=SessionMetricsResponse)
async def get_session_metrics(
    name: str,
    _user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get session metrics: context %, cost, duration, message count."""
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    row = await _require_session_row(db, _user, name=name)
    conv_id = row["conversation_id"] if row else None
    hibernated = bool(row["hibernated"]) if row else False
    auto_hibernate_min = (
        row["auto_hibernate_minutes"] if row and row["auto_hibernate_minutes"] else 30
    )
    session_provider = row["provider"] if row and row["provider"] else "claude"

    # Provider-specific detection for sessions that have not been linked yet.
    if session_provider == "claude" and not conv_id:
        claude_pid = await tmux.get_cli_pid(name)
        if claude_pid:
            conv_id = claude_metrics.detect_conversation_by_pid(claude_pid)
        if not conv_id:
            pane_start = await tmux.get_pane_start_time(name)
            if pane_start:
                conv_id = claude_metrics.detect_conversation_for_session(
                    pane_start,
                    cwd=resolve_project_path(row["project_slug"] if row else None),
                )
    elif session_provider == "codex" and not conv_id:
        codex_pid = await tmux.get_cli_pid(
            name, process_names=get_provider("codex").process_names
        )
        if codex_pid:
            conv_id = await asyncio.to_thread(
                codex_metrics.detect_codex_for_process,
                codex_pid,
                (),
            )
        if not conv_id:
            pane_start = await tmux.get_pane_start_time(name)
            pane_cwd = await tmux.get_pane_cwd(name)
            conv_id = await asyncio.to_thread(
                codex_metrics.detect_codex_for_session,
                pane_start,
                pane_cwd,
                (),
            )

    if not conv_id:
        return SessionMetricsResponse(
            hibernated=hibernated, auto_hibernate_minutes=auto_hibernate_min
        )

    provider = get_metrics_provider(session_provider)
    if provider is None:
        return SessionMetricsResponse(
            conversation_id=conv_id,
            hibernated=hibernated,
            auto_hibernate_minutes=auto_hibernate_min,
        )

    conv_cwd = None
    if session_provider == "claude":
        conv_cwd = await asyncio.to_thread(
            _resolve_conversation_cwd,
            conv_id,
            row["project_slug"] if row else None,
        )
        if not conv_cwd:
            return SessionMetricsResponse(
                conversation_id=conv_id,
                hibernated=hibernated,
                auto_hibernate_minutes=auto_hibernate_min,
            )

    metrics = await asyncio.to_thread(provider.parse_session, conv_id, conv_cwd)
    if not metrics:
        return SessionMetricsResponse(
            conversation_id=conv_id,
            hibernated=hibernated,
            auto_hibernate_minutes=auto_hibernate_min,
        )

    return SessionMetricsResponse(
        conversation_id=conv_id,
        model=metrics.model,
        context_pct=metrics.context_pct,
        cost_usd=metrics.cost_usd,
        message_count=metrics.message_count,
        duration_minutes=metrics.duration_minutes,
        hibernated=hibernated,
        auto_hibernate_minutes=auto_hibernate_min,
        # PR2 dual metrics
        context_pct_real=metrics.context_pct_real,
        context_pct_scaled=metrics.context_pct_scaled,
        cost_conversation_usd=metrics.cost_conversation_usd,
        cost_session_usd=metrics.cost_session_usd,
        cost_session_incomplete=metrics.cost_session_incomplete,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        reasoning_tokens=metrics.reasoning_tokens,
        working_seconds_msg=metrics.working_seconds_msg,
        pricing_version=metrics.pricing_version,
        # PR4 shadow cost
        cost_conversation_equivalent_usd=metrics.cost_conversation_equivalent_usd,
        cost_session_equivalent_usd=metrics.cost_session_equivalent_usd,
        cost_equivalent_pricing_version=metrics.cost_equivalent_pricing_version,
    )


@router.post("/{name}/hibernate", status_code=200)
@_session_operation("hibernate")
async def hibernate_session(
    name: str,
    _user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Hibernate a session: exit CLI, preserve conversation for resume."""
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    workspace_id = _workspace_for_user(_user)
    row = await _require_session_row(db, _user, name=name)
    has_workspace = await _sessions_have_workspace(db)
    session_where = "name = ? AND workspace_id = ?" if has_workspace else "name = ?"
    session_params = (name, workspace_id) if has_workspace else (name,)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="Session not found")

    if row and row["hibernated"]:
        raise HTTPException(status_code=409, detail="Session already hibernated")

    session_provider = row["provider"] if row and row["provider"] else "claude"
    provider_config = get_provider(session_provider)
    is_claude = session_provider == "claude"

    # Detect conversation_id before hibernating
    conv_id = row["conversation_id"] if row else None
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
                    cwd=resolve_project_path(row["project_slug"] if row else None),
                )
    elif session_provider == "opencode":
        launch_spec = build_session_start_spec(
            session_provider,
            row["project_slug"] if row else None,
            row["launch_model"] if row else None,
            row["permission_preset"] if row else None,
            session_name=name,
        )
        conv_id = await _resolve_opencode_session_id(
            db=db,
            name=name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            stored_session_id=conv_id,
            created_at=row["created_at"] if row else None,
        )

    # Get metrics before hibernating (Claude only)
    metrics = None
    if is_claude and conv_id:
        conv_cwd = await asyncio.to_thread(
            _resolve_conversation_cwd,
            conv_id,
            row["project_slug"] if row else None,
        )
        if conv_cwd:
            metrics = await asyncio.to_thread(
                claude_metrics.find_conversation_by_id, conv_id, conv_cwd
            )

    # Send provider-specific exit sequence
    status = await tmux.get_session_status(name)
    if status and status in provider_config.process_names:
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
        # PR3: drop legacy last_context_pct write — maintenance loop owns
        # last_context_pct_real / _scaled via Step 2.5.
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

    params.extend(session_params)
    await db.execute(
        f"UPDATE sessions_meta SET {', '.join(updates)} "
        f"WHERE {session_where}",
        params,
    )
    await db.commit()

    logger.info(
        "Session hibernated: %s (conversation=%s, provider=%s)",
        name,
        conv_id,
        session_provider,
    )
    return {"status": "hibernated", "conversation_id": conv_id}


@router.post("/{name}/resume", status_code=200)
@_session_operation("resume")
async def resume_session(
    name: str,
    _user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Resume a hibernated session: restart CLI with --resume (Claude) or fresh start (others)."""
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    workspace_id = _workspace_for_user(_user)
    row = await _require_session_row(db, _user, name=name)
    has_workspace = await _sessions_have_workspace(db)
    session_where = "name = ? AND workspace_id = ?" if has_workspace else "name = ?"
    session_params = (name, workspace_id) if has_workspace else (name,)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="Session not found")

    if not row or not row["hibernated"]:
        raise HTTPException(status_code=409, detail="Session is not hibernated")

    session_provider = row["provider"] if row["provider"] else "claude"
    provider_config = get_provider(session_provider)
    is_claude = session_provider == "claude"
    conv_id = row["conversation_id"]
    launch_model = row["launch_model"] if row else None
    permission_preset = row["permission_preset"] if row else None

    if is_claude:
        # Claude: try to detect conversation_id for --resume
        if not conv_id:
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
            if conv_id:
                await db.execute(
                    "UPDATE sessions_meta SET conversation_id = ? "
                    f"WHERE {session_where}",
                    (conv_id, *session_params),
                )

        project_slug = row["project_slug"] if row else None
        project_path = resolve_project_path(project_slug)
        resume_cwd = project_path
        if conv_id and not _CONVERSATION_ID_RE.match(conv_id):
            logger.warning(
                "Invalid conversation_id format for session %s: %s", name, conv_id
            )
            conv_id = None
        if conv_id:
            found_cwd = await asyncio.to_thread(
                _resolve_conversation_cwd, conv_id, project_slug
            )
            if found_cwd:
                resume_cwd = found_cwd
                if found_cwd != project_path:
                    logger.warning(
                        "Session %s resuming Claude conversation from fallback cwd %s instead of %s",
                        name,
                        found_cwd,
                        project_path,
                    )
            else:
                logger.warning(
                    "Conversation %s for session %s not found under project paths; falling back to --continue",
                    conv_id,
                    name,
                )
                conv_id = None
                await db.execute(
                    "UPDATE sessions_meta SET conversation_id = NULL "
                    f"WHERE {session_where}",
                    session_params,
                )
        if conv_id:
            cmd = build_start_command(
                provider_config, resume_cwd, model=launch_model
            ).replace(
                f"{provider_config.binary} {provider_config.cli_flags}",
                f"claude --resume {conv_id} --dangerously-skip-permissions",
            )
        else:
            cmd = build_start_command(
                provider_config, project_path, model=launch_model
            ).replace(
                f"{provider_config.binary} {provider_config.cli_flags}",
                "claude --continue --dangerously-skip-permissions",
            )
            logger.warning(
                "No conversation_id for session %s, using --continue fallback", name
            )
    elif session_provider == "opencode":
        launch_spec = build_session_start_spec(
            session_provider,
            row["project_slug"] if row else None,
            launch_model,
            permission_preset,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        )
        conv_id = await _resolve_opencode_session_id(
            db=db,
            name=name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            stored_session_id=conv_id,
            created_at=row["created_at"] if row else None,
        )
        cmd = build_session_start_spec(
            session_provider,
            row["project_slug"] if row else None,
            launch_model,
            permission_preset,
            resume_session_id=conv_id,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        ).start_command
    else:
        # Other non-Claude providers still start fresh on resume.
        cmd = build_session_start_spec(
            session_provider,
            row["project_slug"] if row else None,
            launch_model,
            permission_preset,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        ).start_command

    launched_at_ms = int(_time.time() * 1000)
    await tmux.exit_copy_mode(name)
    if session_provider == "opencode":
        await _recreate_tmux_session(name, cmd)
    else:
        await tmux.send_keys(
            name, cmd, double_enter=provider_config.submit_with_double_enter
        )

    if session_provider == "opencode" and not conv_id:
        launch_spec = build_session_start_spec(
            session_provider,
            row["project_slug"] if row else None,
            launch_model,
            permission_preset,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        )
        conv_id = await _capture_new_opencode_session_id(
            db=db,
            name=name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            launched_at_ms=launched_at_ms,
        )

    await db.execute(
        "UPDATE sessions_meta SET hibernated = 0, hibernated_at = NULL "
        f"WHERE {session_where}",
        session_params,
    )
    await db.commit()

    logger.info(
        "Session resumed: %s (conversation=%s, provider=%s)",
        name,
        conv_id,
        session_provider,
    )
    return {"status": "resumed", "conversation_id": conv_id}


@router.post("/{name}/restart", status_code=200)
@_session_operation("restart")
async def restart_session(
    name: str,
    _user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Restart a session: exit and auto-resume last conversation, or start fresh if none."""
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    workspace_id = _workspace_for_user(_user)
    row = await _require_session_row(db, _user, name=name)
    has_workspace = await _sessions_have_workspace(db)
    session_where = "name = ? AND workspace_id = ?" if has_workspace else "name = ?"
    session_params = (name, workspace_id) if has_workspace else (name,)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="Session not found")

    # Detect conversation_id from the already workspace-scoped metadata row.
    conv_id = row["conversation_id"] if row else None
    session_provider = row["provider"] if row and row["provider"] else "claude"
    provider_config = get_provider(session_provider)
    is_claude = session_provider == "claude"
    launch_model = row["launch_model"] if row else None
    permission_preset = row["permission_preset"] if row else None

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
                    cwd=resolve_project_path(row["project_slug"] if row else None),
                )

    # Validate conv_id format (Claude only)
    if is_claude and conv_id and not _CONVERSATION_ID_RE.match(conv_id):
        logger.warning(
            "Invalid conversation_id format for session %s: %s", name, conv_id
        )
        conv_id = None

    status = await tmux.get_session_status(name)
    if status and status in provider_config.process_names:
        if is_claude:
            # Claude-specific exit: C-c, C-u, optional /rename, /exit
            await tmux.send_keys_raw(name, "C-c")
            await asyncio.sleep(1.0)
            await tmux.send_keys_raw(name, "C-u")
            await asyncio.sleep(0.3)
            if not conv_id:
                # Fresh start: rename first to avoid conversation name collisions
                ts_suffix = datetime.now(timezone.utc).strftime("%y%m%d-%H%M")
                rename_label = f"{name}-{ts_suffix}"
                await tmux.send_keys_raw(name, f"/rename {rename_label}")
                await asyncio.sleep(0.3)
                await tmux.send_keys_raw(name, "Enter")
                await asyncio.sleep(1.0)
                await tmux.send_keys_raw(name, "C-u")
                await asyncio.sleep(0.3)
            await tmux.send_keys_raw(name, "/exit")
            await asyncio.sleep(0.3)
            await tmux.send_keys_raw(name, "Enter")
            await asyncio.sleep(3.0)
        else:
            # Non-Claude: use provider exit sequence
            for step in provider_config.exit_sequence:
                await tmux.send_keys_raw(name, step.key)
                await asyncio.sleep(step.delay_after)

    # Resume last conversation if available (Claude only), otherwise start fresh
    await tmux.exit_copy_mode(name)
    project_slug = row["project_slug"] if row else None
    project_path = resolve_project_path(project_slug)
    resume_cwd = project_path
    if conv_id:
        found_cwd = await asyncio.to_thread(
            _resolve_conversation_cwd, conv_id, project_slug
        )
        if found_cwd:
            resume_cwd = found_cwd
            if found_cwd != project_path:
                logger.warning(
                    "Session %s restarting Claude conversation from fallback cwd %s instead of %s",
                    name,
                    found_cwd,
                    project_path,
                )
        else:
            logger.warning(
                "Conversation %s for session %s not found under project paths; restarting fresh",
                conv_id,
                name,
            )
            conv_id = None
    if is_claude and conv_id:
        cmd = build_start_command(
            provider_config, resume_cwd, model=launch_model
        ).replace(
            f"{provider_config.binary} {provider_config.cli_flags}",
            f"claude --resume {conv_id} --dangerously-skip-permissions",
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
        conv_id = await _resolve_opencode_session_id(
            db=db,
            name=name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            stored_session_id=conv_id,
            created_at=row["created_at"] if row else None,
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
    launched_at_ms = int(_time.time() * 1000)
    if is_claude:
        await tmux.send_keys(
            name, cmd, double_enter=provider_config.submit_with_double_enter
        )
    else:
        # Non-Claude restarts recreate the tmux session to avoid stale TUI state.
        await _recreate_tmux_session(name, cmd)

    if session_provider == "opencode" and not conv_id:
        launch_spec = build_session_start_spec(
            session_provider,
            project_slug,
            launch_model,
            permission_preset,
            session_name=name,
            theme_mode=row["theme_mode"] if row else None,
        )
        conv_id = await _capture_new_opencode_session_id(
            db=db,
            name=name,
            workspace_id=workspace_id,
            launch_dir=launch_spec.launch_dir,
            launched_at_ms=launched_at_ms,
        )

    # Update DB: clear metrics. Clear conversation_id only on fresh start.
    # PR3: clear dual columns (migration 087) + legacy column for completeness.
    now = datetime.now(timezone.utc).isoformat()
    if conv_id:
        await db.execute(
            """UPDATE sessions_meta SET
                hibernated = 0, hibernated_at = NULL,
                last_context_pct_real = NULL, last_context_pct_scaled = NULL,
                last_context_pct_legacy = NULL,
                last_cost_usd = NULL,
                last_cost_conversation_usd = NULL, last_cost_session_usd = NULL,
                last_cost_conversation_equivalent_usd = NULL,
                last_cost_session_equivalent_usd = NULL,
                last_cost_equivalent_pricing_version = NULL,
                last_message_count = NULL, last_metrics_at = NULL,
                last_active = ?
            WHERE {session_where}""".format(session_where=session_where),
            (now, *session_params),
        )
    else:
        await db.execute(
            """UPDATE sessions_meta SET
                conversation_id = NULL, hibernated = 0, hibernated_at = NULL,
                last_context_pct_real = NULL, last_context_pct_scaled = NULL,
                last_context_pct_legacy = NULL,
                last_cost_usd = NULL,
                last_cost_conversation_usd = NULL, last_cost_session_usd = NULL,
                last_cost_conversation_equivalent_usd = NULL,
                last_cost_session_equivalent_usd = NULL,
                last_cost_equivalent_pricing_version = NULL,
                last_message_count = NULL, last_metrics_at = NULL,
                last_active = ?
            WHERE {session_where}""".format(session_where=session_where),
            (now, *session_params),
        )
    await db.commit()

    logger.info(
        "Session restarted: %s (conversation=%s, resumed=%s)",
        name,
        conv_id,
        bool(conv_id),
    )
    return {
        "status": "restarted",
        "previous_conversation_id": conv_id,
        "resumed": bool(conv_id),
    }


@router.post("/{name}/complete", status_code=200)
@_session_operation("complete")
async def complete_session(
    name: str,
    _user: UserInfo = Depends(get_current_user),
):
    """Complete a session: stamp completed_at, exit Claude Code, kill tmux session.

    Unlike delete (which destroys data), complete preserves session_costs history
    and sets completed_at so the CostsTab can show sessions that finished cleanly.
    """
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Read metadata without holding the writer during tmux/provider I/O.
    workspace_id = _workspace_for_user(_user)
    async with acquire_db() as db:
        row = await _require_session_row(db, _user, name=name)

    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="Session not found")

    conv_id = row["conversation_id"] if row else None
    session_provider = row["provider"] if row and row["provider"] else "claude"
    provider_config = get_provider(session_provider)
    is_claude = session_provider == "claude"

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
                    cwd=resolve_project_path(row["project_slug"] if row else None),
                )

    now = datetime.now(timezone.utc).isoformat()

    # Exit CLI gracefully if running (provider-specific exit sequence)
    status = await tmux.get_session_status(name)
    if status and status in provider_config.process_names:
        for step in provider_config.exit_sequence:
            await tmux.send_keys_raw(name, step.key)
            await asyncio.sleep(step.delay_after)

    # Kill tmux session
    success = await tmux.kill_session(name)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to kill session")

    cost_row = None
    async with write_db() as db:
        if _allow_legacy_local_session_schema():
            await db.execute(
                "UPDATE sessions_meta SET workspace_id = ? WHERE name = ?",
                (workspace_id, name),
            )
            await db.execute(
                "UPDATE session_costs SET workspace_id = ? "
                "WHERE workspace_id IS NULL AND session_name = ?",
                (workspace_id, name),
            )
        if conv_id:
            await db.execute(
                "UPDATE session_costs SET completed_at = ? WHERE conversation_id = ? "
                "AND workspace_id = ? AND session_name = ?",
                (now, conv_id, workspace_id, name),
            )

            cursor = await db.execute(
                "SELECT cost_usd, input_tokens, output_tokens, message_count "
                "FROM session_costs WHERE conversation_id = ? "
                "AND workspace_id = ? AND session_name = ?",
                (conv_id, workspace_id, name),
            )
            cost_row = await cursor.fetchone()

        await db.execute(
            "UPDATE session_costs SET session_name = NULL WHERE session_name = ? "
            "AND workspace_id = ? AND completed_at IS NOT NULL",
            (name, workspace_id),
        )
        await db.execute(
            "DELETE FROM sessions_meta WHERE name = ? AND workspace_id = ?",
            (name, workspace_id),
        )

        await db.commit()

    _invalidate_sessions_cache()
    asyncio.create_task(
        _broadcast_session_event("destroyed", workspace_id=workspace_id)
    )

    # Build recap from session_costs
    recap: dict = {"conversation_id": conv_id, "completed_at": now}
    if cost_row:
        recap.update(
            {
                "cost_usd": round(cost_row["cost_usd"], 4),
                "input_tokens": cost_row["input_tokens"] or 0,
                "output_tokens": cost_row["output_tokens"] or 0,
                "message_count": cost_row["message_count"],
                "working_seconds": row["working_seconds"] if row else 0,
            }
        )

    logger.info("Session completed: %s (conversation=%s)", name, conv_id)
    return {"status": "completed", **recap}


@router.post("/{name}/send-message", status_code=200)
async def send_message_to_session(
    name: str,
    body: SendMessageBody,
    _user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Send a message to a session via tmux send-keys.

    Polls until the CLI is ready, then injects the text using tmux send-keys.
    Uses double Enter for Claude Code (multiline input) and single Enter for others.
    """
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    row = await _require_session_row(db, _user, name=name)
    if not await tmux.session_exists(name):
        raise HTTPException(status_code=404, detail="Session not found")

    # Provider comes from the already workspace-scoped row.
    session_provider = row["provider"] if row and row["provider"] else "claude"
    provider_config = get_provider(session_provider)

    # Poll until CLI is ready (max 15s)
    for _ in range(30):
        status = await tmux.get_session_status(name)
        if status and status in provider_config.process_names:
            break
        await asyncio.sleep(0.5)

    await tmux.send_keys(
        name, body.text, double_enter=provider_config.submit_with_double_enter
    )
    logger.info(
        "Message sent to session %s: %r (provider=%s)",
        name,
        body.text[:80],
        session_provider,
    )
    return {"status": "sent"}


@router.get("/{name}/conversation")
async def get_session_conversation(
    name: str,
    limit: int = 20,
    role: str | None = None,
    since: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    _user: UserInfo = Depends(get_current_user_or_agent),
):
    """Read conversation messages from a CC session JSONL."""
    from core.api.services.conversation_reader import read_conversation

    row = await _require_session_row(db, _user, name=name)
    if (row["provider"] or "claude") != "claude":
        raise HTTPException(
            409, "Conversation history is only available for Claude sessions"
        )
    if not row["conversation_id"]:
        raise HTTPException(404, "No conversation linked to this session")
    try:
        messages = await read_conversation(
            row["conversation_id"], limit=limit, role=role, since=since
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "conversation_id": row["conversation_id"],
        "messages": messages or [],
    }


@router.get("/by-name/{name}", response_model=SessionInfo)
async def get_session_by_name(
    name: str,
    _user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Look up a single session by its tmux name (PR2 — MCP get_session).

    Returns the full SessionInfo including the dual metrics introduced by
    migration 087 (last_context_pct_real/_scaled, last_cost_conversation_usd,
    last_cost_session_usd, input/output/reasoning token counts,
    working_seconds_msg, metrics_refreshed_at, pricing_version).
    """
    try:
        tmux.validate_session_name(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    workspace_id = _workspace_for_user(_user)
    row = await _require_session_row(db, _user, name=name)

    session_provider = row["provider"] if row["provider"] else "claude"
    alive = await tmux.session_exists(name)
    status = await tmux.get_session_status(name) if alive else None
    pane_text = (
        await tmux.capture_pane(name)
        if alive and status in ALL_KNOWN_PROCESS_NAMES
        else None
    )
    activity = (
        tmux.detect_activity_state(pane_text, status, provider=session_provider)
        if pane_text
        else None
    )

    def _opt(col: str):
        return row[col] if row is not None and col in row.keys() else None

    return SessionInfo(
        name=name,
        workspace_id=workspace_id,
        display_name=row["display_name"],
        pinned=bool(row["pinned"]) if row["pinned"] else False,
        sort_order=row["sort_order"] if row["sort_order"] else 0,
        group_name=row["group_name"],
        project_slug=row["project_slug"],
        session_uuid=row["session_uuid"],
        status=status,
        created_at=row["created_at"],
        last_active=row["last_active"],
        attached=False,
        hibernated=bool(row["hibernated"]) if row["hibernated"] else False,
        conversation_id=row["conversation_id"],
        model=row["model"] or row["launch_model"],
        launch_model=row["launch_model"],
        permission_preset=row["permission_preset"],
        last_context_pct=row["last_context_pct"],
        last_cost_usd=row["last_cost_usd"],
        last_message_count=row["last_message_count"],
        auto_hibernate_minutes=row["auto_hibernate_minutes"]
        if row["auto_hibernate_minutes"]
        else 30,
        activity_state=activity,
        working_seconds=row["working_seconds"] if row["working_seconds"] else 0,
        created_epoch=_created_epoch_from_iso(row["created_at"]),
        agent_managed=bool(row["agent_managed"]) if row["agent_managed"] else False,
        owner_id=row["owner_id"] if "owner_id" in row.keys() else None,
        provider=session_provider,
        # PR2 dual metrics
        last_context_pct_real=_opt("last_context_pct_real"),
        last_context_pct_scaled=_opt("last_context_pct_scaled"),
        last_cost_conversation_usd=_opt("last_cost_conversation_usd"),
        last_cost_session_usd=_opt("last_cost_session_usd"),
        last_cost_session_incomplete=bool(_opt("last_cost_session_incomplete") or 0),
        last_input_tokens=_opt("last_input_tokens"),
        last_output_tokens=_opt("last_output_tokens"),
        last_reasoning_tokens=_opt("last_reasoning_tokens"),
        working_seconds_msg=_opt("working_seconds_msg"),
        metrics_refreshed_at=_opt("metrics_refreshed_at"),
        pricing_version=_opt("pricing_version"),
        # PR4 shadow cost (migration 089)
        last_cost_conversation_equivalent_usd=_opt(
            "last_cost_conversation_equivalent_usd"
        ),
        last_cost_session_equivalent_usd=_opt("last_cost_session_equivalent_usd"),
        last_cost_equivalent_pricing_version=_opt(
            "last_cost_equivalent_pricing_version"
        ),
    )


@router.get("/by-uuid/{session_uuid}", response_model=SessionInfo)
async def get_session_by_uuid(
    session_uuid: str = PathParam(..., pattern=_UUID_V4_PATTERN),
    _user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Look up a session by its stable UUID (for permalink support)."""
    workspace_id = _workspace_for_user(_user)
    row = await _require_session_row(
        db, _user, session_uuid=session_uuid
    )

    name = row["name"]
    session_provider = row["provider"] if row["provider"] else "claude"
    alive = await tmux.session_exists(name)
    status = await tmux.get_session_status(name) if alive else None
    pane_text = (
        await tmux.capture_pane(name)
        if alive and status in ALL_KNOWN_PROCESS_NAMES
        else None
    )
    activity = (
        tmux.detect_activity_state(pane_text, status, provider=session_provider)
        if pane_text
        else None
    )

    def _opt(col: str):
        return row[col] if row is not None and col in row.keys() else None

    return SessionInfo(
        name=name,
        workspace_id=workspace_id,
        display_name=row["display_name"],
        pinned=bool(row["pinned"]) if row["pinned"] else False,
        sort_order=row["sort_order"] if row["sort_order"] else 0,
        group_name=row["group_name"],
        project_slug=row["project_slug"],
        session_uuid=row["session_uuid"],
        status=status,
        created_at=row["created_at"],
        last_active=row["last_active"],
        attached=False,
        hibernated=bool(row["hibernated"]) if row["hibernated"] else False,
        conversation_id=row["conversation_id"],
        model=row["model"] or row["launch_model"],
        launch_model=row["launch_model"],
        permission_preset=row["permission_preset"],
        last_context_pct=row["last_context_pct"],
        last_cost_usd=row["last_cost_usd"],
        last_message_count=row["last_message_count"],
        auto_hibernate_minutes=row["auto_hibernate_minutes"]
        if row["auto_hibernate_minutes"]
        else 30,
        activity_state=activity,
        working_seconds=row["working_seconds"] if row["working_seconds"] else 0,
        created_epoch=_created_epoch_from_iso(row["created_at"]),
        agent_managed=bool(row["agent_managed"]) if row["agent_managed"] else False,
        owner_id=row["owner_id"] if "owner_id" in row.keys() else None,
        provider=session_provider,
        # PR2 dual metrics
        last_context_pct_real=_opt("last_context_pct_real"),
        last_context_pct_scaled=_opt("last_context_pct_scaled"),
        last_cost_conversation_usd=_opt("last_cost_conversation_usd"),
        last_cost_session_usd=_opt("last_cost_session_usd"),
        last_cost_session_incomplete=bool(_opt("last_cost_session_incomplete") or 0),
        last_input_tokens=_opt("last_input_tokens"),
        last_output_tokens=_opt("last_output_tokens"),
        last_reasoning_tokens=_opt("last_reasoning_tokens"),
        working_seconds_msg=_opt("working_seconds_msg"),
        metrics_refreshed_at=_opt("metrics_refreshed_at"),
        pricing_version=_opt("pricing_version"),
        # PR4 shadow cost (migration 089)
        last_cost_conversation_equivalent_usd=_opt(
            "last_cost_conversation_equivalent_usd"
        ),
        last_cost_session_equivalent_usd=_opt("last_cost_session_equivalent_usd"),
        last_cost_equivalent_pricing_version=_opt(
            "last_cost_equivalent_pricing_version"
        ),
    )
