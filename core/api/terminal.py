# v1.6.0 - 2026-03-29 - Generalized file upload (any type, project /input/ dir, streaming)
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import json
import logging
import os
import shutil
import struct
import termios
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import aiofiles
from fastapi import (
    APIRouter,
    Depends,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from core.api.config import settings
from core.api.db import acquire_db
from core.api.models import UserInfo
from core.api.routers._adapter import to_http
from core.api.routers._browser_mutation_denial import agent_only_route
from core.api.security import (
    TerminalTicketPrincipal,
    consume_ws_ticket_principal,
    get_agent_user,
)
from core.api.services.runas import runas_user
from core.api.services.terminal_metrics import TerminalMetricsCollector
from core.api.services.tmux import (
    get_pane_cwd,
    resolve_session_server,
    tmux_command_for_server,
    tmux_env_for_server,
    validate_session_name,
)
from core.api.services.ingest.watcher import enqueue_file
from core.api.services.project_lifecycle import isolated_project_file_write
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_CONNECTIONS_PER_SESSION = 5
PTY_READ_SIZE = 4096
HEARTBEAT_INTERVAL = 30
IDLE_TIMEOUT = 1800  # 30 minutes
MAX_MESSAGE_SIZE = 64 * 1024  # 64KB — large enough for any realistic paste
FALLBACK_UPLOAD_DIR = Path("/tmp/pir-uploads")
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB
UPLOAD_CHUNK_SIZE = 64 * 1024  # 64KB
TMUX_CAPTURE_TIMEOUT = 10
PROJECTS_ROOT = Path("/data/projects")

_ws_send_locks: dict[int, asyncio.Lock] = {}


def _local_terminal_compatibility() -> bool:
    return settings.deploy_mode == "core" and not settings.multi_tenant_enabled


def _ws_send_lock(ws: WebSocket) -> asyncio.Lock:
    key = id(ws)
    lock = _ws_send_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ws_send_locks[key] = lock
    return lock


def _drop_ws_send_lock(ws: WebSocket) -> None:
    _ws_send_locks.pop(id(ws), None)


async def _send_ws_text(ws: WebSocket, message: str, *, timeout: float = 2.0) -> None:
    async with _ws_send_lock(ws):
        await asyncio.wait_for(ws.send_text(message), timeout=timeout)


async def _send_ws_bytes(ws: WebSocket, data: bytes, *, timeout: float = 2.0) -> None:
    async with _ws_send_lock(ws):
        await asyncio.wait_for(ws.send_bytes(data), timeout=timeout)


async def _close_ws(
    ws: WebSocket,
    *,
    code: int = 1000,
    reason: str | None = None,
    timeout: float = 2.0,
) -> None:
    try:
        async with _ws_send_lock(ws):
            await asyncio.wait_for(ws.close(code=code, reason=reason), timeout=timeout)
    finally:
        _drop_ws_send_lock(ws)


def _metrics_collector(
    ws: WebSocket, workspace_id: str | None = None
) -> TerminalMetricsCollector | None:
    if workspace_id is None:
        return None
    if workspace_id == "ws_default":
        collector = getattr(ws.app.state, "terminal_metrics", None)
        if not isinstance(collector, TerminalMetricsCollector):
            collector = TerminalMetricsCollector()
            ws.app.state.terminal_metrics = collector
        return collector
    collectors = getattr(ws.app.state, "terminal_metrics_by_workspace", None)
    if not isinstance(collectors, dict):
        collectors = {}
        ws.app.state.terminal_metrics_by_workspace = collectors
    collector = collectors.get(workspace_id)
    if not isinstance(collector, TerminalMetricsCollector):
        collector = TerminalMetricsCollector()
        collectors[workspace_id] = collector
    return collector


async def _terminal_binding_is_active(
    db, principal: TerminalTicketPrincipal
) -> bool:
    """Revalidate the ticket's user/session/workspace tuple before PTY attach."""
    cursor = await db.execute("PRAGMA table_info(sessions_meta)")
    session_columns = {row["name"] for row in await cursor.fetchall()}
    if "workspace_id" in session_columns:
        session_row = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta WHERE name = ? AND workspace_id = ?",
                (principal.session_name, principal.workspace_id),
            )
        ).fetchone()
    elif _local_terminal_compatibility():
        session_row = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta WHERE name = ?",
                (principal.session_name,),
            )
        ).fetchone()
    else:
        return False

    if session_row is None:
        return False
    if _local_terminal_compatibility() and principal.user_id == "local":
        return True
    user_row = await (
        await db.execute(
            "SELECT 1 FROM users WHERE workspace_id = ? AND deleted_at IS NULL "
            "AND (id = ? OR slug = ?)",
            (principal.workspace_id, principal.user_id, principal.username),
        )
    ).fetchone()
    return user_row is not None


async def _capture_pane_snapshot(
    name: str,
    *,
    collector: TerminalMetricsCollector | None = None,
) -> str | None:
    """Capture the currently visible tmux pane with ANSI escapes intact.

    Instrumented for the cold->hot observability gate: every invocation records
    its wall-clock duration, outcome (ok/empty/timeout/error/no_server) and the
    captured byte count, both as a structured log line and (when a metrics
    collector is reachable) via TerminalMetricsCollector.record_capture_pane.
    """
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        logger.info(
            "capture_pane: session=%s ms=0.0 outcome=no_server bytes=0", name
        )
        if collector is not None:
            collector.record_capture_pane(
                session_name=name,
                duration_ms=0.0,
                outcome="no_server",
                bytes_captured=0,
            )
        return None

    started = time.perf_counter()
    outcome = "error"
    bytes_captured = 0
    snapshot: str | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *tmux_command_for_server(
                server,
                "capture-pane",
                "-t",
                name,
                "-p",
                "-e",
            ),
            env=tmux_env_for_server(server),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=TMUX_CAPTURE_TIMEOUT
        )
        if proc.returncode == 0:
            decoded = stdout.decode("utf-8", errors="replace")
            if decoded:
                snapshot = decoded
                bytes_captured = len(stdout)
                outcome = "ok"
            else:
                outcome = "empty"
        else:
            outcome = "error"
    except asyncio.TimeoutError:
        outcome = "timeout"
    except OSError:
        outcome = "error"
    finally:
        capture_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "capture_pane: session=%s ms=%.1f outcome=%s bytes=%d",
            name,
            capture_ms,
            outcome,
            bytes_captured,
        )
        if collector is not None:
            collector.record_capture_pane(
                session_name=name,
                duration_ms=capture_ms,
                outcome=outcome,
                bytes_captured=bytes_captured,
            )
    return snapshot


def _session_safe_slug(slug: str) -> str:
    """Mirror the tmux session-name sanitization applied to project slugs.

    A tmux session name cannot contain '&' (nor other shell metacharacters), so a
    real project slug such as ``c&i-master`` surfaces as the session-safe form
    ``c-i-master``. Any character outside the session-safe set ``[a-z0-9_-]``
    collapses to '-'. This is intentionally distinct from the KG sanitization
    (``&``->``_``/removed) and must not be conflated with it.
    """
    return "".join(char if (char.isalnum() or char in "_-") else "-" for char in slug)


def _resolve_registered_slug(slug: str) -> tuple[str | None, bool]:
    """Resolve a (possibly session-sanitized) slug to a real registered project.

    Returns ``(resolved_slug_or_None, index_available)``:
      - direct hit: ``slug`` is itself a registered project slug;
      - alias hit: exactly one registered slug whose session-safe form == ``slug``
        (recovers ``c&i-master`` from the sanitized ``c-i-master``);
      - miss: ``None``.
    ``index_available`` reports whether a non-empty project index was available to
    check against; when it is empty/cold the caller cannot verify registration and
    should trust the slug as-is rather than reject it.
    """
    from core.api.routers import projects as _projects
    import time as _t

    if _t.monotonic() - _projects._index_built_at > _projects._INDEX_TTL:
        _projects._build_project_index()
    index = _projects._project_index
    if not index:
        return None, False
    if slug in index:
        return slug, True
    matches = [registered for registered in index if _session_safe_slug(registered) == slug]
    if len(matches) == 1:
        return matches[0], True
    return None, True


def _resolve_upload_target(
    project_slug: str | None, pane_cwd: str | None
) -> tuple[str | None, Path]:
    """Resolve the best upload directory for a terminal session.

    Prefer the session project when known. If the DB row is contaminated/missing,
    try to recover the project from the pane cwd. Otherwise fall back to the
    generic upload directory instead of hard-failing the UI.

    The candidate slug is resolved against the registered project index before it
    is used to build a filesystem path: a session-sanitized alias (``c-i-master``)
    is remapped to its real slug (``c&i-master``), and a slug that matches no
    registered project is rejected to the fallback dir instead of spawning a
    phantom ``/data/projects/<slug>/`` tree.
    """
    if not project_slug and pane_cwd:
        from core.api.routers.sessions import _detect_project_from_path

        project_slug = _detect_project_from_path(pane_cwd)

    if project_slug:
        resolved, index_available = _resolve_registered_slug(project_slug)
        if resolved is None and index_available:
            # Slug matches no registered project (e.g. a stale tmux-sanitized
            # alias). Don't create a phantom project dir — fall back.
            return None, FALLBACK_UPLOAD_DIR
        effective_slug = resolved or project_slug
        return effective_slug, Path(f"/data/projects/{effective_slug}/input")

    return None, FALLBACK_UPLOAD_DIR


def _safe_upload_segment(value: str | None, fallback: str = "manual") -> str:
    raw = (value or fallback).strip()
    cleaned = "".join(
        char if char.isalnum() or char in "._-" else "-" for char in raw
    ).strip("-._")
    return (cleaned or fallback)[:80]


def _terminal_attachment_paths(
    project_slug: str, session: str | None
) -> tuple[Path, Path]:
    """Return absolute and project-relative stable upload dirs for terminal refs."""
    projects_root = PROJECTS_ROOT.resolve()
    project_root = (projects_root / project_slug).resolve()
    if not project_root.is_relative_to(projects_root):
        raise ValueError("Resolved project path escaped projects root")
    relative_dir = (
        Path("attachments")
        / "terminal"
        / _safe_upload_segment(session, fallback="manual")
    )
    absolute_dir = (project_root / relative_dir).resolve()
    if not absolute_dir.is_relative_to(project_root):
        raise ValueError("Resolved terminal attachment path escaped project root")
    return absolute_dir, relative_dir


@asynccontextmanager
async def _terminal_project_write_guard(
    user: UserInfo,
    *,
    workspace_id: str,
    project_slug: str | None,
    session: str | None,
):
    if project_slug is None:
        yield
        return
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    if ctx.workspace_id != workspace_id:
        ctx = CallerContext(
            username=ctx.username,
            system_role=ctx.system_role,
            user_type=ctx.user_type,
            workspace_id=workspace_id,
            scopes=ctx.scopes,
            is_human_session=False,
            user_id=ctx.user_id,
            local_runtime=ctx.local_runtime,
        )
    try:
        async with isolated_project_file_write(
            ctx,
            project_slug=project_slug,
            writer_kind="terminal_upload",
            resource_ref=f"terminal:{session or 'manual'}",
            projects_root=PROJECTS_ROOT,
        ):
            yield
    except ServiceError as exc:
        raise to_http(exc) from exc


@agent_only_route(router, "/terminal/upload", methods=["POST"])
async def upload_file(
    file: UploadFile,
    _user: UserInfo = Depends(get_agent_user),
    session: str | None = Query(default=None),
):
    """Upload a file and return its server-side path.

    When a session name is provided, resolves the project_slug from sessions_meta,
    saves a stable terminal reference under /data/projects/{project_slug}/attachments/,
    and copies the same bytes to /data/projects/{project_slug}/input/ for Ingester.
    If the DB metadata is missing/contaminated, tries to recover the project from
    the tmux pane cwd and otherwise falls back to /tmp/pir-uploads/ instead of
    failing silently in the UI.
    """
    # Sanitize filename: extract basename, strip null bytes, reject dot-prefix
    raw_name = PurePosixPath(file.filename or "upload").name
    raw_name = raw_name.replace("\x00", "")
    if not raw_name or raw_name.startswith("."):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid filename"},
        )

    workspace_id = (_user.workspace_id or "").strip()
    if not workspace_id:
        if not _local_terminal_compatibility():
            return JSONResponse(status_code=404, content={"detail": "Upload not found"})
        workspace_id = "ws_default"
    workspace_fallback_dir = (
        FALLBACK_UPLOAD_DIR
        if _local_terminal_compatibility()
        else FALLBACK_UPLOAD_DIR / _safe_upload_segment(workspace_id, "workspace")
    )

    # Determine upload directory based on session -> project_slug
    project_slug: str | None = None
    if session:
        try:
            validate_session_name(session)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid session name"},
            )
        from core.api.db import acquire_db

        async with acquire_db() as db:
            columns_cursor = await db.execute("PRAGMA table_info(sessions_meta)")
            session_columns = {
                row["name"] for row in await columns_cursor.fetchall()
            }
            session_has_workspace = "workspace_id" in session_columns
            if session_has_workspace:
                cursor = await db.execute(
                    "SELECT project_slug FROM sessions_meta "
                    "WHERE workspace_id = ? AND name = ?",
                    (workspace_id, session),
                )
            elif _local_terminal_compatibility():
                cursor = await db.execute(
                    "SELECT project_slug FROM sessions_meta WHERE name = ?",
                    (session,),
                )
            else:
                cursor = None
            row = await cursor.fetchone() if cursor is not None else None
            if row is None and not _local_terminal_compatibility():
                return JSONResponse(
                    status_code=404, content={"detail": "Session not found"}
                )
            stored_project_slug = row[0] if row else None

        pane_cwd = await get_pane_cwd(session) if not stored_project_slug else None
        project_slug, upload_dir = _resolve_upload_target(stored_project_slug, pane_cwd)
        if project_slug is None:
            upload_dir = workspace_fallback_dir

        if project_slug:
            async with acquire_db() as db:
                try:
                    project_row = await (
                        await db.execute(
                            "SELECT COUNT(DISTINCT workspace_id) AS workspace_count, "
                            "MAX(CASE WHEN workspace_id = ? THEN 1 ELSE 0 END) AS owned "
                            "FROM workspace_projects WHERE project_slug = ?",
                            (workspace_id, project_slug),
                        )
                    ).fetchone()
                except Exception:
                    project_row = None
            workspace_count = (
                int(project_row["workspace_count"] or 0) if project_row else 0
            )
            project_owned = bool(project_row and project_row["owned"])
            if not (
                workspace_count == 1
                and project_owned
                or (_local_terminal_compatibility() and workspace_count == 0)
            ):
                return JSONResponse(
                    status_code=404, content={"detail": "Project not found"}
                )

        if project_slug and not stored_project_slug:
            from core.api.db import acquire_write_db

            async with acquire_write_db() as db:
                if _local_terminal_compatibility():
                    if session_has_workspace:
                        await db.execute(
                            "UPDATE sessions_meta SET "
                            "project_slug = COALESCE(project_slug, ?), workspace_id = ? "
                            "WHERE name = ?",
                            (project_slug, workspace_id, session),
                        )
                    else:
                        await db.execute(
                            "UPDATE sessions_meta SET "
                            "project_slug = COALESCE(project_slug, ?) WHERE name = ?",
                            (project_slug, session),
                        )
                else:
                    await db.execute(
                        "UPDATE sessions_meta SET project_slug = COALESCE(project_slug, ?) "
                        "WHERE workspace_id = ? AND name = ?",
                        (project_slug, workspace_id, session),
                    )
                await db.commit()
    else:
        upload_dir = workspace_fallback_dir

    ingest_dir = upload_dir
    project_relative_path: str | None = None
    ingest_path: Path | None = None

    if project_slug:
        try:
            upload_dir, project_relative_dir = _terminal_attachment_paths(
                project_slug, session
            )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid project upload path"},
            )

    async with _terminal_project_write_guard(
        _user,
        workspace_id=workspace_id,
        project_slug=project_slug,
        session=session,
    ):
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Timestamp prefix for dedup
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        filename = f"{ts}_{raw_name}"
        filepath = (upload_dir / filename).resolve()

        # Path traversal guard
        if not filepath.is_relative_to(upload_dir.resolve()):
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid filename (path traversal)"},
            )

        # Stream to disk in chunks (avoid loading entire file into memory)
        total_size = 0
        async with aiofiles.open(filepath, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE:
                    await f.close()
                    filepath.unlink(missing_ok=True)
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "File too large (max 100MB)"},
                    )
                await f.write(chunk)

        if project_slug:
            ingest_dir.mkdir(parents=True, exist_ok=True)
            ingest_path = (ingest_dir / filename).resolve()
            if not ingest_path.is_relative_to(ingest_dir.resolve()):
                filepath.unlink(missing_ok=True)
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid filename (path traversal)"},
                )
            shutil.copy2(filepath, ingest_path)
            project_relative_path = (project_relative_dir / filename).as_posix()

    logger.info(
        "File uploaded: %s (%d bytes, project=%s, ingest_path=%s)",
        filepath,
        total_size,
        project_slug or "none",
        ingest_path or "none",
    )
    if project_slug and ingest_path:
        try:
            await enqueue_file(
                ingest_path,
                workspace_id=workspace_id,
                source_kind="terminal_upload",
            )
        except Exception:
            logger.exception("failed to enqueue terminal upload: %s", ingest_path)
    response = {
        "path": str(filepath),
        "filename": filename,
        "size": total_size,
        "project": project_slug or "",
    }
    if project_relative_path:
        response["project_relative_path"] = project_relative_path
    if ingest_path:
        response["ingest_path"] = str(ingest_path)
    return response


@dataclass
class TerminalSession:
    """Represents an active PTY connection to a tmux session."""

    name: str
    master_fd: int
    pid: int
    workspace_id: str = "ws_default"
    connections: set = field(default_factory=set)
    # snapshot_pending gates the PTY fanout from pushing live bytes to a
    # ws until the cold-attach capture-pane replay frame has been delivered.
    # (Plan A v2 also reused this set as a race gate; that path is gone now —
    # the only remaining producer is the initial join in handle_websocket.)
    snapshot_pending_connections: set = field(default_factory=set)
    last_input_time: float = field(default_factory=time.time)
    _reader_task: asyncio.Task | None = None


class SessionManager:
    """Manages active terminal sessions and their PTY connections."""

    def __init__(self):
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _apply_winsize(master_fd: int, cols: int, rows: int) -> None:
        """Apply PTY dimensions (rows, cols) to the active tmux client."""
        fcntl.ioctl(
            master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )

    @staticmethod
    def _needs_fresh_pty(session: TerminalSession) -> bool:
        """A finished reader means the tmux attach PTY already exited."""
        return session._reader_task is not None and session._reader_task.done()

    async def attach(
        self,
        session_name: str,
        ws: WebSocket,
        cols: int = 80,
        rows: int = 24,
        *,
        workspace_id: str = "ws_default",
    ) -> TerminalSession:
        """Attach a WebSocket to a terminal session. Creates PTY if needed."""
        if not workspace_id.strip():
            raise ConnectionError("Workspace context required")
        async with self._lock:
            if session_name in self._sessions:
                session = self._sessions[session_name]
                if session.workspace_id != workspace_id:
                    raise ConnectionError("Session not found")
                if self._needs_fresh_pty(session):
                    await self._cleanup_session(session)
                    del self._sessions[session_name]
                    if workspace_id == "ws_default":
                        session = await self._create_pty(session_name, cols, rows)
                    else:
                        session = await self._create_pty(
                            session_name, cols, rows, workspace_id=workspace_id
                        )
                    session.workspace_id = workspace_id
                    session.connections.add(ws)
                    session.snapshot_pending_connections.add(ws)
                    self._sessions[session_name] = session
                    logger.info(
                        "PTY recreated after terminated attach-session: session=%s",
                        session_name,
                    )
                    return session
                if len(session.connections) >= MAX_CONNECTIONS_PER_SESSION:
                    raise ConnectionError("Session connection limit reached")
                session.connections.add(ws)
                session.snapshot_pending_connections.add(ws)
                # Resize existing PTY to match new client
                try:
                    self._apply_winsize(session.master_fd, cols, rows)
                except OSError:
                    pass
                return session

            # Create new PTY with correct initial dimensions
            if workspace_id == "ws_default":
                session = await self._create_pty(session_name, cols, rows)
            else:
                session = await self._create_pty(
                    session_name, cols, rows, workspace_id=workspace_id
                )
            session.workspace_id = workspace_id
            session.connections.add(ws)
            session.snapshot_pending_connections.add(ws)
            self._sessions[session_name] = session
            return session

    async def broadcast_session_event(
        self,
        event: str,
        *,
        workspace_id: str | None = None,
        session_name: str | None = None,
        state: str | None = None,
        **extras: Any,
    ) -> None:
        """Notify all connected WS clients that the session list changed.

        Sends a JSON text message to every connected terminal WebSocket.
        Uses 2s timeout per send (proven pattern from jitter fix 2026-03-19).

        Optional `session_name` + `state` ride inline in the payload so the
        frontend can apply the state delta optimistically without a refetch
        round-trip (kills the broadcast-vs-DB-visibility race, julik R1).
        Plan 2026-04-26 PR1, M1.

        `**extras` accept richer payloads (e.g. `event="renamed"` carries
        `old_name`, `new_name`, `session_info` delta). Plan 2026-05-21
        WS broadcast session_renamed delta — closes post-rename stale sidebar.
        """
        payload: dict[str, Any] = {"type": "sessions_changed", "event": event}
        if session_name is not None and state is not None:
            payload["session_name"] = session_name
            payload["state"] = state
        for key, value in extras.items():
            if value is not None:
                payload[key] = value
        await self.broadcast_control_message(payload, workspace_id=workspace_id)

    async def broadcast_control_message(
        self, payload: dict, *, workspace_id: str | None = None
    ) -> None:
        """Send a JSON control message only inside one authenticated workspace."""
        if workspace_id is None:
            if not _local_terminal_compatibility():
                logger.warning("Dropped terminal broadcast without workspace context")
                return
            workspace_id = "ws_default"
        msg = json.dumps(payload)
        async with self._lock:
            for session in self._sessions.values():
                if session.workspace_id != workspace_id:
                    continue
                dead: set = set()
                for ws in list(session.connections):
                    try:
                        await _send_ws_text(ws, msg)
                    except Exception:
                        dead.add(ws)
                session.connections -= dead
                for ws in dead:
                    session.snapshot_pending_connections.discard(ws)
                    _drop_ws_send_lock(ws)

    async def detach(
        self, session_name: str, ws: WebSocket, *, workspace_id: str | None = None
    ) -> None:
        """Detach a WebSocket from a session and clean up the PTY when orphaned.

        The tmux session itself survives independently. We only destroy the
        temporary attach-session PTY proxy so that the next browser reconnect
        gets a fresh attach and a full redraw instead of reusing a half-consumed
        orphan PTY.
        """
        async with self._lock:
            session = self._sessions.get(session_name)
            if not session:
                return
            if workspace_id is not None and session.workspace_id != workspace_id:
                return
            session.connections.discard(ws)
            session.snapshot_pending_connections.discard(ws)
            if not session.connections:
                await self._cleanup_session(session)
                del self._sessions[session_name]
                logger.info(
                    "PTY cleaned up immediately after last client disconnected: session=%s",
                    session_name,
                )

    async def _create_pty(
        self,
        session_name: str,
        cols: int = 80,
        rows: int = 24,
        *,
        workspace_id: str = "ws_default",
    ) -> TerminalSession:
        """Fork and exec tmux attach in a PTY with correct initial dimensions."""
        name = validate_session_name(session_name)
        server = await resolve_session_server(name)
        if server is None:
            server = "marvisx"
        tmux_env = tmux_env_for_server(server)
        tmux_attach_argv = list(
            tmux_command_for_server(server, "attach-session", "-t", name)
        )
        master_fd, slave_fd = os.openpty()

        # Set PTY dimensions BEFORE fork/exec so tmux gets the correct size
        try:
            self._apply_winsize(slave_fd, cols, rows)
        except OSError:
            pass

        # Disable XON/XOFF flow control to prevent Ctrl+S freezing the terminal
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[0] &= ~termios.IXON  # Disable XON/XOFF on input
            attrs[0] &= ~termios.IXOFF  # Disable XON/XOFF on output
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except (termios.error, OSError):
            pass

        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master_fd)
            # login_tty: setsid + set controlling terminal + dup2 stdin/stdout/stderr
            os.login_tty(slave_fd)
            # Set TERM so tmux can attach (systemd services don't inherit TERM)
            os.environ.pop("TMUX", None)
            os.environ.pop("TMUX_TMPDIR", None)
            os.environ.update(tmux_env)
            os.environ["TERM"] = "xterm-256color"
            os.execvpe(tmux_attach_argv[0], tmux_attach_argv, os.environ)
            os._exit(1)  # os._exit, not sys.exit - avoids atexit handlers
        else:
            # Parent process
            os.close(slave_fd)  # Close slave_fd in parent to avoid FD leak
            # Make master_fd non-blocking
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            session = TerminalSession(
                name=session_name,
                master_fd=master_fd,
                pid=pid,
                workspace_id=workspace_id,
            )
            logger.info(
                "PTY created for session %s (pid=%d, fd=%d, %dx%d)",
                name,
                pid,
                master_fd,
                cols,
                rows,
            )
            return session

    async def _cleanup_session(self, session: TerminalSession) -> None:
        """Kill PTY process and close file descriptor."""
        if session._reader_task:
            session._reader_task.cancel()
            try:
                await session._reader_task
            except asyncio.CancelledError:
                pass

        try:
            os.kill(session.pid, 9)
        except PermissionError:
            # The session is owned by the repo/session user; retry via that user
            # when the API runs as a distinct service account (no-op otherwise).
            runas = runas_user()
            if runas:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "sudo",
                        "-u",
                        runas,
                        "kill",
                        "-9",
                        str(session.pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except Exception:
                    pass
        except (OSError, ChildProcessError):
            pass
        # Retry waitpid up to 500ms — SIGKILL is asynchronous, process may not
        # have exited yet by the time waitpid is called (the race that creates zombies).
        for _ in range(10):
            try:
                pid_reaped, _ = os.waitpid(session.pid, os.WNOHANG)
                if pid_reaped != 0:
                    break  # reaped successfully
            except (OSError, ChildProcessError):
                break  # already gone or not our child
            await asyncio.sleep(0.05)

        try:
            os.close(session.master_fd)
        except OSError:
            pass

        logger.info("Cleaned up PTY for session %s (pid=%d)", session.name, session.pid)

    async def cleanup_all(self) -> None:
        """Cleanup all sessions on shutdown."""
        async with self._lock:
            for name, session in list(self._sessions.items()):
                for ws in list(session.connections):
                    try:
                        await _close_ws(ws, code=1001, reason="Server shutting down")
                    except Exception:
                        pass
                await self._cleanup_session(session)
            self._sessions.clear()
            logger.info("All terminal sessions cleaned up")


session_manager = SessionManager()

# Track missed pongs per WebSocket (ws can't hold custom attrs)
_ws_missed_pongs: dict[WebSocket, int] = {}


async def kill_orphan_proxies() -> None:
    """Kill orphaned tmux-proxy processes from a previous API instance.

    On startup the SessionManager is empty, so any surviving
    /data/pir/tmux-proxy attach-session processes are orphans
    from the previous API instance that must be cleaned up.
    """
    killed: list[int] = []
    try:
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                exe = os.path.realpath(f"/proc/{proc_dir.name}/exe")
                if exe != "/data/pir/tmux-proxy":
                    continue
                pid = int(proc_dir.name)
                os.kill(pid, 9)
                killed.append(pid)
                logger.info("Startup: killed orphan tmux-proxy PID=%d", pid)
            except (OSError, PermissionError, ValueError):
                continue
    except Exception:
        logger.exception("Error scanning for orphan tmux-proxy processes")
    if killed:
        logger.info(
            "Startup: killed %d orphan tmux-proxy process(es): %s", len(killed), killed
        )
    else:
        logger.debug("Startup: no orphan tmux-proxy processes found")


def reap_zombie_children() -> None:
    """Reap all zombie children synchronously (called from SIGCHLD handler).

    os.waitpid(-1, WNOHANG) reaps any exited child without blocking.
    Loop until no more children are ready to be reaped.
    Called via loop.add_signal_handler(SIGCHLD, reap_zombie_children) in lifespan.
    """
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break  # no more zombie children ready
        except (OSError, ChildProcessError):
            break  # no children at all, or interrupted


@router.websocket("/terminal/ws")
async def terminal_websocket(
    ws: WebSocket,
    ticket: str = "",
    session: str = "",
    cols: int = 80,
    rows: int = 24,
):
    """WebSocket endpoint for terminal access via PTY proxy."""
    # Accept FIRST (FastAPI requires accept before close)
    await ws.accept()
    collector: TerminalMetricsCollector | None = None

    # Origin validation (CORS doesn't cover WebSocket)
    origin = ws.headers.get("origin", "")
    if origin and origin not in settings.cors_origins:
        logger.warning("WS rejected: bad origin %s", origin)
        await _close_ws(ws, code=1008, reason="Origin not allowed")
        return

    if not ticket or not session:
        await _close_ws(ws, code=1008, reason="Missing ticket or session")
        return

    # Validate session name
    try:
        validate_session_name(session)
    except ValueError:
        await _close_ws(ws, code=1008, reason="Invalid session name")
        return

    # Consume ticket (single-use) — in-memory store, no write_lock, no DB.
    consume_started = time.perf_counter()
    lock_wait_ms: float | None = None
    consume_timings: dict[str, float | str] = {}
    try:
        lock_started = time.perf_counter()
        principal = await consume_ws_ticket_principal(
            ticket,
            session,
            timings=consume_timings,
        )
        if principal is not None:
            collector = _metrics_collector(ws, principal.workspace_id)
        lock_wait_ms = (time.perf_counter() - lock_started) * 1000
    except Exception:
        if collector:
            metadata = dict(consume_timings)
            if lock_wait_ms is not None:
                metadata["lock_wait_ms"] = lock_wait_ms
            collector.record_terminal_ticket_event(
                kind="consume",
                session_name=session,
                duration_ms=(time.perf_counter() - consume_started) * 1000,
                outcome="error",
                metadata=metadata,
            )
        raise

    if collector:
        metadata = dict(consume_timings)
        if lock_wait_ms is not None:
            metadata["lock_wait_ms"] = lock_wait_ms
        collector.record_terminal_ticket_event(
            kind="consume",
            session_name=session,
            duration_ms=(time.perf_counter() - consume_started) * 1000,
            outcome=str(
                consume_timings.get("outcome") or ("ok" if principal else "invalid")
            ),
            metadata=metadata,
        )

    if not principal:
        logger.warning("WS rejected: invalid ticket for session %s", session)
        await _close_ws(ws, code=1008, reason="Invalid or expired ticket")
        return

    # Revalidate both sides of the ticket immediately before any tmux/PTY side
    # effect. A deleted user or moved/deleted session invalidates an already
    # issued ticket without revealing which object changed.
    async with acquire_db() as db:
        binding_active = await _terminal_binding_is_active(db, principal)

    if not binding_active:
        logger.warning("WS rejected: stale terminal identity binding")
        await _close_ws(ws, code=1008, reason="Invalid or expired ticket")
        return

    username = principal.username
    workspace_id = principal.workspace_id
    logger.info(
        "WS connected: user=%s workspace=%s session=%s",
        username,
        workspace_id,
        session,
    )

    # Clamp dimensions to sane values
    cols = max(40, min(cols, 500))
    rows = max(10, min(rows, 200))

    # Attach to terminal session with correct initial dimensions
    attached = False
    try:
        term_session = await session_manager.attach(
            session, ws, cols, rows, workspace_id=workspace_id
        )
    except ConnectionError as e:
        await _close_ws(ws, code=1008, reason=str(e))
        return
    attached = True
    if collector:
        collector.websocket_connected(session)

    # PTY output reader
    async def read_pty():
        """Read from PTY using event loop's native I/O (epoll) — no thread pool.

        loop.add_reader() integrates directly with the event loop's epoll/libuv
        selector, waking up immediately when data is available on the PTY fd.
        This eliminates the select()+run_in_executor thread pool overhead and
        scheduling jitter that was adding latency to terminal output.
        """
        loop = asyncio.get_event_loop()
        pty_terminated = False
        if collector:
            collector.pty_reader_started()
        try:
            while True:
                # Wait for PTY fd to become readable via epoll (zero thread overhead).
                readable = asyncio.Event()
                loop.add_reader(term_session.master_fd, readable.set)
                try:
                    await readable.wait()
                finally:
                    loop.remove_reader(term_session.master_fd)

                data = os.read(term_session.master_fd, PTY_READ_SIZE)
                if not data:
                    pty_terminated = True
                    break
                if collector:
                    collector.record_pty_read_bytes(session, len(data))

                dead_connections = set()
                fanout_started = time.perf_counter()
                # Skip ws still receiving the cold-attach capture-pane replay
                # so the first live frame doesn't land before the snapshot.
                ready_connections = (
                    term_session.connections
                    - term_session.snapshot_pending_connections
                )
                connection_count = len(ready_connections)
                for ws in list(ready_connections):
                    try:
                        send_started = time.perf_counter()
                        await _send_ws_bytes(ws, data)
                        if collector:
                            collector.record_pty_write_duration(
                                session,
                                (time.perf_counter() - send_started) * 1000,
                            )
                    except Exception:
                        dead_connections.add(ws)
                if collector:
                    collector.record_fanout_duration(
                        session,
                        (time.perf_counter() - fanout_started) * 1000,
                        connection_count=connection_count,
                    )
                for dead in dead_connections:
                    term_session.connections.discard(dead)
                    term_session.snapshot_pending_connections.discard(dead)
                    _drop_ws_send_lock(dead)
        except (OSError, ValueError):
            pty_terminated = True
        except asyncio.CancelledError:
            pass
        finally:
            if collector:
                collector.pty_reader_stopped()

        if pty_terminated:
            logger.info(
                "PTY terminated for session %s; forcing terminal reconnect", session
            )
            for conn in list(term_session.connections):
                try:
                    await _close_ws(conn, code=1012, reason="PTY terminated")
                except Exception:
                    pass

    # Replay the current pane before this connection joins the live stream so a
    # COLD/fresh reconnect does not stay blank until the next tmux write.
    # `snapshot_pending_connections` keeps an already-running PTY reader from
    # sending live bytes to this websocket before the restore frame is sent.
    reader_running = (
        term_session._reader_task is not None and not term_session._reader_task.done()
    )
    snapshot = await _capture_pane_snapshot(session, collector=collector)
    if snapshot:
        try:
            await _send_ws_bytes(
                ws, f"\x1b[H\x1b[2J{snapshot}".encode("utf-8", errors="replace")
            )
        except Exception:
            pass
    term_session.snapshot_pending_connections.discard(ws)

    # Start PTY reader if not already running. For a fresh COLD attach, the
    # snapshot is sent before the reader starts so the first live stream does
    # not overwrite the restore frame out of order.
    if not reader_running:
        term_session._reader_task = asyncio.create_task(read_pty())

    # Heartbeat
    _ws_missed_pongs[ws] = 0
    pending_ping_sent_at: dict[str, float] = {}

    async def heartbeat():
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                _ws_missed_pongs[ws] = _ws_missed_pongs.get(ws, 0) + 1
                if _ws_missed_pongs[ws] >= 3:
                    logger.warning("WS missed 3 pongs, closing: session=%s", session)
                    dead_connections = {ws}
                    term_session.connections -= dead_connections
                    term_session.snapshot_pending_connections.discard(ws)
                    try:
                        await _close_ws(ws, code=1001, reason="Pong timeout")
                    except Exception:
                        pass
                    break
                ping_id = f"{int(time.time() * 1000)}-{id(ws)}"
                pending_ping_sent_at[ping_id] = time.perf_counter()
                await _send_ws_text(
                    ws,
                    json.dumps(
                        {
                            "type": "ping",
                            "id": ping_id,
                            "sent_at": time.time(),
                        }
                    ),
                )
            except Exception:
                break

    # Idle timeout checker
    async def idle_checker():
        while True:
            await asyncio.sleep(60)
            if time.time() - term_session.last_input_time > IDLE_TIMEOUT:
                logger.info("Idle timeout for session %s", session)
                try:
                    await _close_ws(ws, code=1000, reason="Idle timeout")
                except Exception:
                    pass
                break

    heartbeat_task = asyncio.create_task(heartbeat())
    idle_task = asyncio.create_task(idle_checker())

    try:
        while True:
            message = await ws.receive()

            if message["type"] == "websocket.disconnect":
                break

            if "bytes" in message and message["bytes"]:
                raw = message["bytes"]
                if len(raw) > MAX_MESSAGE_SIZE:
                    await _close_ws(ws, code=1009, reason="Message too large")
                    break

                if len(raw) < 2:
                    continue

                msg_type = raw[0]
                payload = raw[1:]

                if msg_type == 0:  # Input
                    term_session.last_input_time = time.time()
                    try:
                        # Write in chunks to avoid EAGAIN on non-blocking PTY fd.
                        # PTY kernel buffer is ~4096 bytes; yield between chunks so
                        # tmux can drain the slave side before we write more.
                        view = memoryview(payload)
                        offset = 0
                        while offset < len(view):
                            written = os.write(
                                term_session.master_fd, view[offset : offset + 512]
                            )
                            offset += written
                            if offset < len(view):
                                await asyncio.sleep(0)
                    except OSError:
                        break
                elif msg_type == 1:  # Resize
                    try:
                        resize_data = json.loads(payload.decode("utf-8"))
                        cols = max(40, min(int(resize_data.get("cols", 80)), 500))
                        rows = max(10, min(int(resize_data.get("rows", 24)), 200))
                        # struct format: rows, cols (HHHH = unsigned short x4)
                        fcntl.ioctl(
                            term_session.master_fd,
                            termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0),
                        )
                    except (json.JSONDecodeError, ValueError, OSError):
                        pass
                # msg_type == 2 (StreamControl pause/resume) was Plan A v2 —
                # reverted because `tmux capture-pane` (sync read of pane
                # state) races our attach-session PTY reader (async consumer
                # of the same pane's delta stream): the snapshot can ship
                # before kernel-buffered deltas drain, and the next PTY read
                # double-applies them on top → garbled render. Upstream tmux
                # only fixed this in control mode (`tmux -C attach` + flow
                # control via `refresh-client -A pane:pause/continue`, issue
                # #2217). We rely on the client-side Plan B onData guard
                # (isActive/panelVisible/document.hidden) instead.

            elif "text" in message and message["text"]:
                # Handle text control messages
                try:
                    ctrl = json.loads(message["text"])
                    if ctrl.get("type") == "pong":
                        # Pong = browser is still alive; reset idle timer + missed pong counter
                        term_session.last_input_time = time.time()
                        _ws_missed_pongs[ws] = 0
                        ping_id = ctrl.get("id")
                        if collector and isinstance(ping_id, str):
                            sent_at = pending_ping_sent_at.pop(ping_id, None)
                            if sent_at is not None:
                                collector.record_websocket_ping_rtt(
                                    session,
                                    (time.perf_counter() - sent_at) * 1000,
                                )
                except json.JSONDecodeError:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WS error for session %s", session)
    finally:
        heartbeat_task.cancel()
        idle_task.cancel()
        _ws_missed_pongs.pop(ws, None)
        if attached and collector:
            collector.websocket_disconnected(session)
        await session_manager.detach(session, ws, workspace_id=workspace_id)
        _drop_ws_send_lock(ws)
        logger.info("WS disconnected: user=%s session=%s", username, session)
