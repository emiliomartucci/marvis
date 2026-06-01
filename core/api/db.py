# v1.6.0 - 2026-03-15 - Enterprise prerequisites: _column_exists, backup, connection pool + acquire_db() context manager
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid as uuid_mod
from collections import Counter, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, AsyncGenerator

import aiosqlite
from starlette.requests import Request

from core.api.config import settings
from core.api.paths import repo_path

logger = logging.getLogger(__name__)


def _resolve_migrations_dir() -> Path:
    """Locate the ``migrations`` data directory in both layouts.

    In an installed wheel the ``*.sql`` files are shipped as the top-level
    ``migrations`` package-data; ``importlib.resources.files`` resolves them
    correctly regardless of where site-packages lives. ``repo_path`` walking up
    from ``__file__`` only works when the runtime tree mirrors the repo layout
    (learning 9e527cfa: the wheel shipped 0 migrations and the runtime found an
    empty dir without failing). Prefer the resources lookup, fall back to the
    repo layout for an editable/source checkout.
    """
    try:
        import importlib.resources as _res

        candidate = Path(str(_res.files("migrations")))
        if candidate.is_dir():
            return candidate
    except (ModuleNotFoundError, FileNotFoundError, TypeError, AttributeError):
        pass
    return repo_path(__file__, "migrations")


MIGRATIONS_DIR = _resolve_migrations_dir()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table. Used for migration idempotency (partial failure recovery)."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c[1] == column for c in cols)


def get_sync_connection() -> sqlite3.Connection:
    """Synchronous connection for startup migrations."""
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


async def _configure_connection(db: aiosqlite.Connection) -> None:
    """Shared PRAGMAs for all async connections."""
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA synchronous=NORMAL")  # 2-3x faster writes vs FULL
    await db.execute(
        "PRAGMA busy_timeout=15000"
    )  # 15s (5s too aggressive, 30s masks issues)
    await db.execute("PRAGMA cache_size=-64000")  # 64MB
    await db.execute("PRAGMA temp_store=MEMORY")
    await db.execute("PRAGMA mmap_size=536870912")  # 512MB (covers 300MB DB)
    await db.execute("PRAGMA wal_autocheckpoint=1000")  # ~4MB WAL trigger


# --- Single-Writer Architecture ---
# SQLite WAL: one writer + many readers. All writes (router + background) go
# through a single dedicated writer connection serialized by asyncio.Lock.
# Pool connections are read-only (PRAGMA query_only=ON) — writes via pool
# raise OperationalError immediately instead of causing lock contention.
#
# Four primitives:
#   get_db()           — read-only pool (FastAPI DI for GET endpoints)
#   get_write_db()     — writer + lock (FastAPI DI for POST/PATCH/DELETE endpoints)
#   write_db()         — writer + lock + auto-commit (background tasks)
#   acquire_write_db() — writer + lock (WebSocket/non-DI writers)
#   acquire_db()       — read-only pool (WebSocket/non-DI readers)
#
# See: "database is locked" incident 2026-04-12, plan 2026-04-13.

_pool: asyncio.Queue[aiosqlite.Connection] | None = None
_pool_size: int = 0
_writer: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

WRITER_LOCK_EVENT_LIMIT = 500
WRITER_LOCK_SLOW_WAIT_MS = 50.0
WRITER_LOCK_SLOW_HOLD_MS = 1000.0  # Fase 0: log writer holds >1s to separate offenders from queued victims
_writer_metrics_lock = Lock()
_writer_wait_events: deque[dict[str, Any]] = deque(maxlen=WRITER_LOCK_EVENT_LIMIT)
_writer_hold_events: deque[dict[str, Any]] = deque(maxlen=WRITER_LOCK_EVENT_LIMIT)
_writer_current_holder: dict[str, Any] | None = None
_writer_sequence = 0


async def init_pool(size: int = 2) -> None:
    """Create read-only connection pool + dedicated writer. Call once in lifespan startup.

    Pool connections have PRAGMA query_only=ON — any INSERT/UPDATE/DELETE via
    get_db() or acquire_db() raises OperationalError immediately. All writes
    must go through get_write_db(), write_db(), or acquire_write_db().

    Pool size=8 supports concurrent deep=true requests (4 parallel KG subqueries × 2 concurrent requests).
    """
    global _pool, _pool_size, _writer
    actual_size = 8  # read-only pool: expanded for KG lens 4-subquery parallel pattern (Phase 7.0)
    _pool = asyncio.Queue(maxsize=actual_size)
    _pool_size = actual_size
    for _ in range(actual_size):
        db = await aiosqlite.connect(settings.db_path)
        await _configure_connection(db)
        await db.execute("PRAGMA query_only=ON")
        db.row_factory = aiosqlite.Row
        await _pool.put(db)
    # Dedicated writer for ALL writes (router + background), serialized by _write_lock
    _writer = await aiosqlite.connect(settings.db_path)
    await _configure_connection(_writer)
    _writer.row_factory = aiosqlite.Row
    logger.info(
        "DB initialized: %d read-only pool + 1 dedicated writer (single-writer enforced)",
        actual_size,
    )


async def close_pool() -> None:
    """Close all connections. Call in lifespan shutdown."""
    global _pool, _pool_size, _writer
    if _pool:
        closed = 0
        while not _pool.empty():
            try:
                db = _pool.get_nowait()
                await db.close()
                closed += 1
            except asyncio.QueueEmpty:
                break
        _pool = None
        _pool_size = 0
    if _writer:
        await _writer.close()
        _writer = None
    logger.info("DB connections closed")


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Read-only pool connection for request handlers. For writes use get_write_db()."""
    if _pool is not None:
        db = await _pool.get()
        try:
            yield db
        finally:
            try:
                await _pool.put(db)
            except asyncio.QueueFull:
                await db.close()
    else:
        db = await aiosqlite.connect(settings.db_path)
        await _configure_connection(db)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()


@asynccontextmanager
async def acquire_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Read-only pool access outside FastAPI DI (e.g. WebSocket handlers). For writes use acquire_write_db()."""
    if _pool is not None:
        db = await _pool.get()
        try:
            yield db
        finally:
            try:
                await _pool.put(db)
            except asyncio.QueueFull:
                await db.close()
    else:
        db = await aiosqlite.connect(settings.db_path)
        await _configure_connection(db)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def _writer_owner_label(label: str | None = None) -> str:
    if label:
        return label

    repo_root = Path(__file__).parent.parent
    current_file = Path(__file__).resolve()
    frame = inspect.currentframe()
    if frame is not None:
        frame = frame.f_back
    try:
        while frame is not None:
            frame_path = Path(frame.f_code.co_filename).resolve()
            if frame_path != current_file:
                try:
                    rel_path = frame_path.relative_to(repo_root)
                except ValueError:
                    frame = frame.f_back
                    continue
                return f"{rel_path}:{frame.f_code.co_name}:{frame.f_lineno}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def _current_writer_blocker(now_perf: float) -> dict[str, Any] | None:
    with _writer_metrics_lock:
        if not _writer_current_holder:
            return None
        return {
            "label": _writer_current_holder["label"],
            "task_name": _writer_current_holder.get("task_name"),
            "held_ms": max(
                0.0, (now_perf - _writer_current_holder["started_perf"]) * 1000
            ),
        }


def _start_writer_hold(
    *,
    label: str,
    task_name: str | None,
    queued_at: float,
    acquired_at: float,
    wait_ms: float,
    blocked_by: dict[str, Any] | None,
) -> dict[str, Any]:
    global _writer_current_holder, _writer_sequence
    with _writer_metrics_lock:
        _writer_sequence += 1
        holder = {
            "id": _writer_sequence,
            "label": label,
            "task_name": task_name,
            "queued_at": queued_at,
            "acquired_at": acquired_at,
            "started_perf": time.perf_counter(),
            "wait_ms": wait_ms,
            "blocked_by": blocked_by,
        }
        _writer_current_holder = holder
        _writer_wait_events.append(
            {
                "label": label,
                "task_name": task_name,
                "queued_at": queued_at,
                "acquired_at": acquired_at,
                "wait_ms": wait_ms,
                "contended": wait_ms >= 1.0,
                "slow": wait_ms >= WRITER_LOCK_SLOW_WAIT_MS,
                "blocked_by": blocked_by,
            }
        )
    if wait_ms >= WRITER_LOCK_SLOW_WAIT_MS:
        logger.warning(
            "SQLite writer lock WAIT %.0fms for %s (blocked_by=%s)",
            wait_ms,
            label,
            (blocked_by or {}).get("label"),
        )
    return holder


def _finish_writer_hold(holder: dict[str, Any]) -> None:
    global _writer_current_holder
    ended_at = time.time()
    hold_ms = max(0.0, (time.perf_counter() - holder["started_perf"]) * 1000)
    with _writer_metrics_lock:
        if _writer_current_holder and _writer_current_holder.get("id") == holder["id"]:
            _writer_current_holder = None
        _writer_hold_events.append(
            {
                "label": holder["label"],
                "task_name": holder.get("task_name"),
                "acquired_at": holder["acquired_at"],
                "ended_at": ended_at,
                "hold_ms": hold_ms,
                "wait_ms": holder["wait_ms"],
                "blocked_by": holder.get("blocked_by"),
            }
        )
    if hold_ms >= WRITER_LOCK_SLOW_HOLD_MS:
        logger.warning(
            "SQLite writer lock HELD %.0fms by %s (waited %.0fms) — offender, not victim",
            hold_ms,
            holder["label"],
            holder.get("wait_ms", 0.0),
        )


def get_writer_lock_snapshot(window_seconds: float = 60.0) -> dict[str, Any]:
    """Return rolling telemetry for the global SQLite writer lock."""
    now = time.time()
    now_perf = time.perf_counter()
    cutoff = now - window_seconds
    with _writer_metrics_lock:
        waits = [
            event for event in _writer_wait_events if event["queued_at"] >= cutoff
        ]
        holds = [
            event for event in _writer_hold_events if event["ended_at"] >= cutoff
        ]
        current_holder = dict(_writer_current_holder) if _writer_current_holder else None

    wait_values = [float(event["wait_ms"]) for event in waits]
    hold_values = [float(event["hold_ms"]) for event in holds]
    wait_by_label: dict[str, list[float]] = defaultdict(list)
    hold_by_label: dict[str, list[float]] = defaultdict(list)
    blocked_by_labels: Counter[str] = Counter()
    for event in waits:
        wait_by_label[str(event["label"])].append(float(event["wait_ms"]))
        blocked_by = event.get("blocked_by")
        if isinstance(blocked_by, dict) and blocked_by.get("label"):
            blocked_by_labels[str(blocked_by["label"])] += 1
    for event in holds:
        hold_by_label[str(event["label"])].append(float(event["hold_ms"]))

    if current_holder:
        current_holder["held_ms"] = max(
            0.0, (now_perf - current_holder.pop("started_perf")) * 1000
        )

    return {
        "window_seconds": window_seconds,
        "locked": _write_lock.locked(),
        "current_holder": current_holder,
        "wait_ms": _summary(wait_values),
        "hold_ms": _summary(hold_values),
        "contended_wait_count": sum(1 for value in wait_values if value >= 1.0),
        "slow_wait_count": sum(
            1 for value in wait_values if value >= WRITER_LOCK_SLOW_WAIT_MS
        ),
        "wait_by_label": {
            label: _summary(values) for label, values in sorted(wait_by_label.items())
        },
        "hold_by_label": {
            label: _summary(values) for label, values in sorted(hold_by_label.items())
        },
        "blocked_by_label_counts": dict(blocked_by_labels),
        "last_wait_events": waits[-10:],
        "last_hold_events": holds[-10:],
    }


def reset_writer_lock_metrics_for_tests() -> None:
    global _writer_current_holder
    with _writer_metrics_lock:
        _writer_wait_events.clear()
        _writer_hold_events.clear()
        _writer_current_holder = None


def _request_writer_label(request: Request | None) -> str:
    if request is None:
        return "get_write_db"

    scope = request.scope
    route = scope.get("route")
    route_path = getattr(route, "path", None) or scope.get("path") or "unknown"
    method = scope.get("method") or request.method or "REQUEST"
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return f"{method} {route_path}"

    module = getattr(endpoint, "__module__", None)
    qualname = getattr(endpoint, "__qualname__", None) or getattr(
        endpoint, "__name__", None
    )
    endpoint_name = ".".join(part for part in (module, qualname) if part)
    if not endpoint_name:
        return f"{method} {route_path}"
    return f"{method} {route_path} -> {endpoint_name}"


@asynccontextmanager
async def _acquire_writer(
    *, label: str | None = None
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Internal: acquire write lock, yield writer, rollback on error.

    All public write primitives (write_db, get_write_db, acquire_write_db)
    delegate here for consistent lock + rollback semantics.
    """
    owner_label = _writer_owner_label(label)
    task = asyncio.current_task()
    task_name = task.get_name() if task else None
    queued_at = time.time()
    queued_perf = time.perf_counter()
    blocked_by = _current_writer_blocker(queued_perf) if _write_lock.locked() else None
    await _write_lock.acquire()
    acquired_at = time.time()
    holder = _start_writer_hold(
        label=owner_label,
        task_name=task_name,
        queued_at=queued_at,
        acquired_at=acquired_at,
        wait_ms=(time.perf_counter() - queued_perf) * 1000,
        blocked_by=blocked_by,
    )
    try:
        if not _writer:
            raise RuntimeError("DB not initialized — call init_pool() first")
        try:
            yield _writer
        except Exception:
            await _writer.rollback()
            raise
    finally:
        _finish_writer_hold(holder)
        _write_lock.release()


@asynccontextmanager
async def write_db(
    label: str | None = None,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Background tasks: auto-commit on success, auto-rollback on error.

    Use for metrics_collector, cost_service, security_collector,
    event_dispatcher — any periodic background write.

    Do NOT do slow work (HTTP calls, computation) inside this context.
    Gather data first, then write in a fast batch.
    """
    async with _acquire_writer(label=label) as w:
        yield w
        await w.commit()


async def get_write_db(
    request: Request,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Router write endpoints: caller must commit. Auto-rollback on error.

    Use Depends(get_write_db) for any endpoint that does INSERT/UPDATE/DELETE.
    The pool connection (get_db) is read-only — writes will fail with
    OperationalError: attempt to write a readonly database.
    """
    async with _acquire_writer(label=_request_writer_label(request)) as w:
        yield w


@asynccontextmanager
async def acquire_write_db(
    label: str | None = None,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """WebSocket/non-DI writers: caller must commit. Auto-rollback on error.

    Use this for code that needs to write outside FastAPI dependency injection
    (e.g. WebSocket handlers, terminal upload).
    """
    async with _acquire_writer(label=label) as w:
        yield w


async def wal_checkpoint() -> tuple[int, int, int]:
    """Run PRAGMA wal_checkpoint(TRUNCATE) via the writer connection.

    Returns (busy, log, checkpointed) — same as SQLite's checkpoint result row.
    busy>0 means active readers blocked a full truncate (partial checkpoint still ran).
    """
    async with _acquire_writer(label="wal_checkpoint") as writer:
        cursor = await writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = await cursor.fetchone()
        return (row[0], row[1], row[2])


# sqlite-vec support
_vec_table_ready = False

# Platform-specific loadable-extension suffixes. The vec0 loadable is `.so` on
# Linux (prod), `.dylib` on macOS, `.dll` on Windows.
_VEC0_SUFFIXES = (".so", ".dylib", ".dll")


def resolve_vec0_loadable() -> tuple[str | None, bool]:
    """Resolve the vec0 loadable path, cross-platform.

    Returns ``(load_arg, found)`` where ``load_arg`` is the argument to pass to
    ``SELECT load_extension(?)`` and ``found`` is True when a real loadable file
    exists on disk.

    Resolution (prod-safe by construction):
      1. ``settings.vec0_path`` when it resolves to a real file: if it already
         carries a known suffix use it as-is, otherwise probe ``.so`` /
         ``.dylib`` / ``.dll``. Prod (Linux, ``/data/pir/lib/vec0`` →
         ``vec0.so``, set via ``VEC0_PATH``) resolves here and keeps using the
         exact configured path.
      2. Else the installed ``sqlite_vec`` package's own loadable
         (``sqlite_vec.loadable_path()``) — the OSS clean-install path, where
         ``/data/pir/lib/vec0`` is absent and the bundled loadable is
         platform-correct (.dylib on macOS, no longer rejected by a hardcoded
         ``.so`` check).

    The suffix-less ``load_arg`` lets SQLite append the platform suffix itself.
    """
    vec_path = Path(settings.vec0_path)
    if vec_path.suffix:
        if vec_path.exists():
            return (str(vec_path), True)
    else:
        for suffix in _VEC0_SUFFIXES:
            if vec_path.with_suffix(suffix).exists():
                # Pass the suffix-less path; SQLite appends the platform suffix.
                return (str(vec_path), True)

    try:
        import sqlite_vec  # type: ignore

        pkg_path = Path(str(sqlite_vec.loadable_path()))
        if pkg_path.exists():
            return (str(pkg_path), True)
        # loadable_path() returns a suffix-less base on some builds; probe.
        for suffix in _VEC0_SUFFIXES:
            if pkg_path.with_suffix(suffix).exists():
                return (str(pkg_path), True)
    except Exception:  # noqa: BLE001 — package missing/old → settings path is final
        pass

    # Nothing resolved; return the configured path so callers log a clear miss.
    return (str(vec_path), False)


async def ensure_vec_documents(db: aiosqlite.Connection) -> bool:
    """Load sqlite-vec on an existing connection and ensure vec_documents exists."""
    global _vec_table_ready
    load_arg, found = resolve_vec0_loadable()
    if not found or load_arg is None:
        return False

    if not getattr(db, "_pir_vec_extension_loaded", False):
        await db._execute(db._conn.enable_load_extension, True)
        await db.execute("SELECT load_extension(?)", [load_arg])
        setattr(db, "_pir_vec_extension_loaded", True)
    if not _vec_table_ready:
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents USING vec0(
                doc_id INTEGER PRIMARY KEY,
                embedding float[512]
            )
        """)
        _vec_table_ready = True
    return True


async def get_vec_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Dedicated dependency for sqlite-vec endpoints. Mirrors get_db() PRAGMAs + loads vec0."""
    db = await aiosqlite.connect(settings.db_path)
    await _configure_connection(db)
    db.row_factory = aiosqlite.Row
    await ensure_vec_documents(db)
    try:
        yield db
    finally:
        await db.close()


def run_migrations() -> None:
    """Apply pending SQL migrations in order."""
    conn = get_sync_connection()
    try:
        # Ensure schema_versions exists for first run
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_versions "
            "(version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor = conn.execute("SELECT MAX(version) FROM schema_versions")
        row = cursor.fetchone()
        current_version = row[0] if row[0] is not None else 0

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        pending = [
            f
            for f in migration_files
            if not f.stem.endswith("_down")
            and int(f.stem.split("_")[0]) > current_version
        ]

        # Pre-migration backup (enterprise rollback strategy)
        # Keep only last 2 backups to prevent disk fill (was unbounded)
        if pending:
            backup_path = f"{settings.db_path}.backup-v{current_version}"
            try:
                shutil.copy2(settings.db_path, backup_path)
                logger.info("Pre-migration backup: %s", backup_path)
                # Rotate: keep only the 2 most recent backups
                import glob

                db_dir = os.path.dirname(settings.db_path) or "."
                db_name = os.path.basename(settings.db_path)
                backups = sorted(
                    glob.glob(os.path.join(db_dir, f"{db_name}.backup-v*")),
                    key=os.path.getmtime,
                )
                for old_backup in backups[:-2]:
                    try:
                        os.remove(old_backup)
                        logger.info("Rotated old backup: %s", old_backup)
                    except OSError:
                        pass
            except OSError as e:
                logger.warning("Pre-migration backup failed (continuing): %s", e)

        for migration_file in migration_files:
            if migration_file.stem.endswith("_down"):
                continue
            version = int(migration_file.stem.split("_")[0])
            if version > current_version:
                logger.info("Applying migration %s", migration_file.name)
                sql = migration_file.read_text()
                conn.executescript(sql)
                # executescript() resets PRAGMA foreign_keys; re-enable
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute(
                    "INSERT OR IGNORE INTO schema_versions (version) VALUES (?)",
                    (version,),
                )
                conn.commit()

                # Post-migration hooks
                if version == 8:
                    _backfill_session_uuids(conn)
                if version == 16:
                    _seed_users_and_migrate_owner(conn)
                if version == 18:
                    _seed_agents(conn)
                if version == 45:
                    _add_documents_columns(conn)
                if version == 46:
                    _add_salience_columns(conn)
                if version == 47:
                    _seed_missing_agents(conn)
                if version == 48:
                    _fix_agent_paths_and_roles(conn)
                if version == 49:
                    _migration_049_agent_role_and_learnings(conn)
                if version == 58:
                    _backfill_inbox_status_from_treatment(conn)
                if version == 59:
                    _add_deep_research_column(conn)
                    _cleanup_generic_source_scores(conn)
                if version == 60:
                    _add_sent_in_newsletter_column(conn)
                if version == 61:
                    _migration_061_backfill_sources(conn)
                if version == 62:
                    _migration_062_backfill_from_urls(conn)
                if version == 63:
                    _add_task_completion_mode(conn)
                if version == 70:
                    _migration_070_digest_ranking_inputs_recovery(conn)
                if version == 71:
                    _migration_071_digest_selection_recovery(conn)
                if version == 72:
                    _migration_072_digest_app_settings_recovery(conn)
                if version == 102:
                    _promote_llm_costs_columns(conn)
                if version == 135:
                    _migration_135_graph_edges_provider(conn)
                if version == 136:
                    _backfill_documents_fts(conn)

                logger.info("Migration %s applied", migration_file.name)

        if not _column_exists(conn, "sessions_meta", "theme_mode"):
            _add_session_theme_mode_column(conn)

        logger.info("Database at version %d", max(current_version, 0))
    finally:
        conn.close()


def _backfill_session_uuids(conn: sqlite3.Connection) -> None:
    """Backfill session_uuid for existing sessions (migration 008 post-hook)."""
    cursor = conn.execute("SELECT name FROM sessions_meta WHERE session_uuid IS NULL")
    rows = cursor.fetchall()
    if not rows:
        return
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[0]
        conn.execute(
            "UPDATE sessions_meta SET session_uuid = ? WHERE name = ?",
            (str(uuid_mod.uuid4()), name),
        )
    conn.commit()
    logger.info("Backfilled UUIDs for %d sessions", len(rows))


def _seed_users_and_migrate_owner(conn: sqlite3.Connection) -> None:
    """Migration 016 post-hook: seed the admin user + data-migrate tasks.owner_id.

    Runs synchronously inside run_migrations() — do NOT use await here.
    Accepts either PIR_ADMIN_PASSWORD_HASH (pre-hashed, preferred in production)
    or PIR_PASSWORD (plaintext, will be bcrypt-hashed here).
    At least one must be set or the migration aborts.

    The admin identity is config-driven via the same MARVIS_ADMIN_* vars used by
    the deploy bootstrap (scripts/init.sh), with generic defaults, so a fresh
    install seeds no hardcoded name. On an existing deployment this seed is
    skipped (users already exist), so the defaults never affect a running install.
    """
    # Prefer pre-hashed password (production .env has PIR_ADMIN_PASSWORD_HASH)
    hashed = os.environ.get("PIR_ADMIN_PASSWORD_HASH", "").strip()
    if not hashed:
        import bcrypt

        pir_password = os.environ.get("PIR_PASSWORD", "").strip()
        if not pir_password:
            raise RuntimeError(
                "Migration 016 requires PIR_ADMIN_PASSWORD_HASH or PIR_PASSWORD env var "
                "to seed the admin user. Set at least one in .env."
            )
        hashed = bcrypt.hashpw(pir_password.encode("utf-8"), bcrypt.gensalt()).decode()

    admin_id = os.environ.get("MARVIS_ADMIN_USER_ID", "").strip() or "usr_admin"
    admin_slug = os.environ.get("MARVIS_ADMIN_SLUG", "").strip() or "admin"
    admin_name = os.environ.get("MARVIS_ADMIN_DISPLAY_NAME", "").strip() or "Admin"

    cursor = conn.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    if user_count == 0:
        conn.execute(
            "INSERT OR IGNORE INTO users "
            "(id, slug, display_name, type, password_hash, system_role) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, admin_slug, admin_name, "human", hashed, "super_admin"),
        )
        conn.commit()
        logger.info("Migration 016: seeded admin user '%s' (super_admin)", admin_slug)

    # Data migration: owner_id may contain slug strings (e.g. "emilio") from before
    # the users table existed. Resolve each slug to the corresponding users.id.
    # Values without a matching slug are left unchanged (NULL FK, graceful fallback).
    conn.execute("""
        UPDATE tasks
        SET owner_id = (SELECT id FROM users WHERE slug = tasks.owner_id)
        WHERE owner_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM users WHERE slug = tasks.owner_id)
    """)
    conn.commit()
    logger.info("Migration 016: owner_id slug→id data migration complete")


def _seed_agents(conn: sqlite3.Connection) -> None:
    """Migration 018 post-hook: seed the deploy's configured system agents.

    The agent slugs come from settings.static_agent_identities (deploy .env), so
    OSS core hardcodes no tenant agent names. A fresh OSS install with no config
    seeds nothing here (no internal agents). On prod this migration already ran;
    rows persist independently and re-running is inert (INSERT OR IGNORE).

    IDs follow the convention usr_{slug} / agt-{slug} (same as seed_agent_users.py
    and migration 047). All rows are idempotent.
    """
    for slug in settings.static_agent_identities:
        usr_id = f"usr_{slug}"
        agt_id = f"agt-{slug}"
        conn.execute(
            "INSERT OR IGNORE INTO users (id, slug, display_name, type, avatar_color, system_role, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', '#3B82F6', 'operator', datetime('now','utc'), datetime('now','utc'))",
            (usr_id, slug, slug),
        )
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, user_id, scheduler_agent_id, agent_type, model, status, description, created_at, updated_at) "
            "VALUES (?, ?, ?, 'system', 'sonnet', 'active', ?, datetime('now','utc'), datetime('now','utc'))",
            (agt_id, usr_id, slug, f"{slug} agent"),
        )
    conn.commit()
    logger.info(
        "Migration 018: seeded %d configured system agent(s)",
        len(settings.static_agent_identities),
    )


def _add_documents_columns(conn: sqlite3.Connection) -> None:
    """Migration 045 post-hook: add doc_type, doc_title, workspace_id to documents (idempotent).

    This runs AFTER conn.executescript(sql) so we can safely ALTER TABLE and CREATE INDEX.
    The SQL file only does the schema_versions INSERT to avoid index-on-missing-column errors.
    """
    if not _column_exists(conn, "documents", "doc_type"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'handoff'"
        )
        logger.info("Migration 045: added documents.doc_type")
    if not _column_exists(conn, "documents", "doc_title"):
        conn.execute("ALTER TABLE documents ADD COLUMN doc_title TEXT")
        logger.info("Migration 045: added documents.doc_title")
    if not _column_exists(conn, "documents", "workspace_id"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'ws_default'"
        )
        logger.info("Migration 045: added documents.workspace_id")
    # Index must be created after the column exists (can't be in SQL file)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type)"
    )
    conn.commit()


def _add_salience_columns(conn: sqlite3.Connection) -> None:
    """Migration 046 post-hook: add salience, archived, salience_updated_at to documents (idempotent).

    Same pattern as migration 045 — SQL file only does schema_versions INSERT + boost_log table.
    ALTER TABLE + partial indexes run here after columns exist.
    """
    if not _column_exists(conn, "documents", "salience"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN salience REAL NOT NULL DEFAULT 0.5"
        )
        logger.info("Migration 046: added documents.salience")
    if not _column_exists(conn, "documents", "archived"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("Migration 046: added documents.archived")
    if not _column_exists(conn, "documents", "salience_updated_at"):
        conn.execute("ALTER TABLE documents ADD COLUMN salience_updated_at TEXT")
        logger.info("Migration 046: added documents.salience_updated_at")
    # Partial indexes for active (non-archived) documents
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_active ON documents(doc_type) WHERE archived = 0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_salience ON documents(salience DESC, doc_type) WHERE archived = 0"
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _backfill_documents_fts(conn: sqlite3.Connection) -> None:
    """Migration 135 post-hook: backfill full-text bodies for documents_fts.

    SQL migrations cannot read filesystem bodies. The SQL file creates the FTS5
    table, trigger sync, and a file_path-only fallback. This hook replaces that
    fallback with the full body for loadable files and row-backed document
    sources, while staying idempotent through DELETE + INSERT by rowid.
    """
    if not _table_exists(conn, "documents_fts") or not _table_exists(conn, "documents"):
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    title_expr = "doc_title" if "doc_title" in columns else "file_path AS doc_title"
    salience_expr = "salience" if "salience" in columns else "0.5 AS salience"
    archived_filter = "WHERE COALESCE(archived, 0) = 0" if "archived" in columns else ""
    rows = conn.execute(
        f"""SELECT id, file_path, project, {title_expr}, {salience_expr}
            FROM documents
            {archived_filter}"""
    ).fetchall()

    for row in rows:
        doc_id = int(row["id"])
        title = row["doc_title"] or row["file_path"] or ""
        content = _documents_fts_content(conn, row)
        conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
        conn.execute(
            "INSERT INTO documents_fts(rowid, doc_id, title, content) VALUES (?, ?, ?, ?)",
            (doc_id, doc_id, title, content),
        )
    conn.commit()
    logger.info("Migration 135: backfilled documents_fts rows=%d", len(rows))


def _documents_fts_content(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    file_path = row["file_path"] or ""
    title = row["doc_title"] or file_path
    project = row["project"] or ""

    if file_path.startswith("task:"):
        task_id = file_path.split(":", 1)[1]
        task = _fetch_one_or_none(
            conn,
            "SELECT title, description, status, project, tags FROM tasks WHERE id = ?",
            (task_id,),
        )
        if task is not None:
            return "\n".join(
                str(part)
                for part in (
                    task["title"],
                    task["description"],
                    f"Status: {task['status']}",
                    f"Project: {task['project']}",
                    f"Tags: {task['tags']}",
                )
                if part
            )

    if file_path.startswith("learning:"):
        learning_id = file_path.split(":", 1)[1]
        learning = _fetch_one_or_none(
            conn,
            "SELECT title, description, prevention, category, severity, tags "
            "FROM learnings WHERE id = ?",
            (learning_id,),
        )
        if learning is not None:
            return "\n".join(
                str(part)
                for part in (
                    learning["title"],
                    learning["description"],
                    f"Prevention: {learning['prevention']}",
                    f"Category: {learning['category']}",
                    f"Severity: {learning['severity']}",
                    f"Tags: {learning['tags']}",
                )
                if part
            )

    if file_path.startswith("inbox_item:"):
        inbox_id = file_path.split(":", 1)[1]
        inbox = _fetch_one_or_none(
            conn,
            "SELECT title, content, tldr, source, status FROM inbox_items WHERE id = ?",
            (inbox_id,),
        )
        if inbox is not None:
            return "\n".join(
                str(part)
                for part in (
                    inbox["title"],
                    inbox["content"],
                    f"TLDR: {inbox['tldr']}",
                    f"Source: {inbox['source']}",
                    f"Status: {inbox['status']}",
                )
                if part
            )

    if file_path.startswith("/") and _is_loadable_document_path(file_path):
        try:
            return Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    return "\n".join(part for part in (str(title), str(project), str(file_path)) if part)


def _fetch_one_or_none(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> sqlite3.Row | None:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None


def _is_loadable_document_path(file_path: str) -> bool:
    path = Path(file_path)
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size <= 500_000
    except OSError:
        return False


def _add_task_completion_mode(conn: sqlite3.Connection) -> None:
    """Migration 063 post-hook: add tasks.completion_mode (idempotent).

    Values: 'pr' (default, requires merged PR), 'doc' (research/brainstorm/plan),
    'none' (verify/diagnose/free transition).

    Backfill heuristic for existing in_progress tasks: scan title/tags for
    research/brainstorm/plan/verify keywords and set completion_mode='doc'
    so Fix 2 cleanup can close them via normal PATCH. All other existing rows
    stay on default 'pr' (backward compat — code fixes keep the strict guard).
    """
    if not _column_exists(conn, "tasks", "completion_mode"):
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN completion_mode TEXT NOT NULL DEFAULT 'pr'"
        )
        logger.info("Migration 063: added tasks.completion_mode")

        # Backfill existing in_progress tasks that look like research/planning work.
        # This unblocks Fix 2 cleanup of the 32 orphan tasks without manual PATCH.
        # Heuristic: title starts with research/brainstorm/plan/verify/diagnose/analyze/investigate
        # OR any tag in research-y set. Conservative — only in_progress rows.
        research_keywords = (
            "research",
            "brainstorm",
            "plan",
            "verify",
            "verifi",
            "diagnose",
            "diagnost",
            "analyze",
            "analizza",
            "investigate",
            "indaga",
            "indagar",
        )
        research_tags = {
            "research",
            "brainstorm",
            "plan",
            "planning",
            "verification",
            "verify",
            "investigation",
            "diagnostics",
            "analysis",
        }

        cursor = conn.execute(
            "SELECT id, title, tags FROM tasks WHERE status = 'in_progress'"
        )
        rows = cursor.fetchall()
        backfilled = 0
        for row in rows:
            task_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
            title = (row["title"] if isinstance(row, sqlite3.Row) else row[1]) or ""
            tags_raw = row["tags"] if isinstance(row, sqlite3.Row) else row[2]
            title_lc = title.lower()
            try:
                tags_list = set(json.loads(tags_raw)) if tags_raw else set()
            except (json.JSONDecodeError, TypeError):
                tags_list = set()
            matches_title = any(
                title_lc.startswith(k) or f" {k}" in title_lc for k in research_keywords
            )
            matches_tags = bool(tags_list & research_tags)
            if matches_title or matches_tags:
                conn.execute(
                    "UPDATE tasks SET completion_mode = 'doc' WHERE id = ?",
                    (task_id,),
                )
                backfilled += 1
        conn.commit()
        logger.info(
            "Migration 063: backfilled %d in_progress tasks to completion_mode='doc'",
            backfilled,
        )


def _migration_070_digest_ranking_inputs_recovery(conn: sqlite3.Connection) -> None:
    """Recovery migration for digest ranking inputs on DBs already past version 69."""
    if not _column_exists(conn, "inbox_items", "domain_key"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN domain_key TEXT")
        logger.info("Migration 070: added inbox_items.domain_key")
    if not _column_exists(conn, "inbox_items", "published_at"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN published_at TEXT")
        logger.info("Migration 070: added inbox_items.published_at")
    if not _column_exists(conn, "inbox_items", "freshness_at"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN freshness_at TEXT")
        logger.info("Migration 070: added inbox_items.freshness_at")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inbox_items_workspace_domain_freshness "
        "ON inbox_items(workspace_id, domain_key, freshness_at DESC, created_at DESC)"
    )
    conn.commit()


def _migration_071_digest_selection_recovery(conn: sqlite3.Connection) -> None:
    """Recovery migration for inbox_digest_selections on DBs already past version 69."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS inbox_digest_selections ("
        "id TEXT PRIMARY KEY, "
        "inbox_item_id TEXT NOT NULL, "
        "digest_cycle_key TEXT NOT NULL, "
        "state TEXT NOT NULL CHECK (state IN ('visible', 'overflow', 'expired')), "
        "domain_key TEXT NOT NULL, "
        "score REAL NOT NULL DEFAULT 0, "
        "rank_in_domain INTEGER, "
        "expires_at TEXT, "
        "workspace_id TEXT NOT NULL DEFAULT 'ws_default', "
        "created_at TEXT DEFAULT (datetime('now','utc')), "
        "updated_at TEXT DEFAULT (datetime('now','utc')), "
        "FOREIGN KEY (inbox_item_id) REFERENCES inbox_items(id) ON DELETE CASCADE"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_selection_item_cycle "
        "ON inbox_digest_selections(workspace_id, inbox_item_id, digest_cycle_key)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_selection_active_item "
        "ON inbox_digest_selections(workspace_id, inbox_item_id) "
        "WHERE state IN ('visible', 'overflow')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_selection_cycle_state_domain "
        "ON inbox_digest_selections(workspace_id, digest_cycle_key, state, domain_key, rank_in_domain)"
    )
    conn.commit()


def _migration_072_digest_app_settings_recovery(conn: sqlite3.Connection) -> None:
    """Recovery migration for digest app_settings defaults on DBs already past version 69."""
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_enabled', 'shadow')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_freeze_hour_utc', '6')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_admission_threshold', '1.0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_overflow_ttl_days', '3')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_last_cycle_key', '')"
    )
    conn.commit()


def _seed_missing_agents(conn: sqlite3.Connection) -> None:
    """Migration 047 post-hook: seed DevX, System Health, Reddit agents (idempotent).

    Must be in Python hook (not SQL) because executescript() + PRAGMA foreign_keys=ON
    causes FK constraint errors when inserting users + agents in the same script.
    """
    agents_base = settings.effective_agents_base
    # (usr_id, slug, display, color, agt_id, agt_type, model, desc, agent_dir)
    agents = [
        (
            "usr_devx",
            "devx",
            "DevX",
            "#EF4444",
            "agt-devx",
            "system",
            "sonnet",
            "DevX Session Monitor",
            "devx",
        ),
        (
            "usr_system_health",
            "system-health",
            "System Health",
            "#10B981",
            "agt-system-health",
            "system",
            "haiku",
            "System Health Check",
            "system-monitor",
        ),
        (
            "usr_reddit",
            "reddit",
            "Reddit",
            "#F97316",
            "agt-reddit",
            "system",
            "haiku",
            "Reddit Morning Digest",
            "reddit",
        ),
    ]
    for (
        usr_id,
        slug,
        display,
        color,
        agt_id,
        agt_type,
        model,
        desc,
        agent_dir,
    ) in agents:
        soul_path = f"{agents_base}/{agent_dir}/SOUL.md"
        tools_path = f"{agents_base}/{agent_dir}/TOOLS.md"
        identity_path = f"{agents_base}/{agent_dir}/IDENTITY.md"
        conn.execute(
            "INSERT OR IGNORE INTO users (id, slug, display_name, type, avatar_color, system_role, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', ?, 'operator', datetime('now','utc'), datetime('now','utc'))",
            [usr_id, slug, display, color],
        )
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, user_id, scheduler_agent_id, agent_type, model, status, description, "
            "soul_path, tools_path, identity_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, datetime('now','utc'), datetime('now','utc'))",
            [
                agt_id,
                usr_id,
                slug,
                agt_type,
                model,
                desc,
                soul_path,
                tools_path,
                identity_path,
            ],
        )
    conn.commit()
    logger.info("Migration 047: seeded DevX + System Health + Reddit agents")


def _fix_agent_paths_and_roles(conn: sqlite3.Connection) -> None:
    """Migration 048 post-hook: fix soul_path/tools_path for devx, system-health, reddit, analyst + system_role."""
    agents_base = settings.effective_agents_base
    # Fix paths for devx, system-health, reddit (Bug 1: were NULL)
    path_fixes = [
        ("agt-devx", "devx"),
        ("agt-system-health", "system-monitor"),
        ("agt-reddit", "reddit"),
    ]
    for agt_id, agent_dir in path_fixes:
        conn.execute(
            "UPDATE agents SET soul_path = ?, tools_path = ?, identity_path = ?, updated_at = datetime('now','utc') WHERE id = ?",
            [
                f"{agents_base}/{agent_dir}/SOUL.md",
                f"{agents_base}/{agent_dir}/TOOLS.md",
                f"{agents_base}/{agent_dir}/IDENTITY.md",
                agt_id,
            ],
        )
    # Fix analyst paths (Bug 2: pointed to .openclaw which is root-only)
    conn.execute(
        "UPDATE agents SET soul_path = ?, tools_path = ?, identity_path = ?, updated_at = datetime('now','utc') WHERE id = 'agt-analyst'",
        [
            f"{agents_base}/analyst/SOUL.md",
            f"{agents_base}/analyst/TOOLS.md",
            f"{agents_base}/analyst/IDENTITY.md",
        ],
    )
    # Fix system_role from 'agent' to 'operator' for the three new agent users (Bug 4)
    conn.execute(
        "UPDATE users SET system_role = 'operator', updated_at = datetime('now','utc') "
        "WHERE id IN ('usr_devx', 'usr_system_health', 'usr_reddit') AND system_role = 'agent'"
    )
    conn.commit()
    logger.info("Migration 048: fixed agent paths + system_role")


def _migration_049_agent_role_and_learnings(conn: sqlite3.Connection) -> None:
    """Migration 049: normalize agent roles + add learnings schema for REM consolidation."""
    # 1. Normalize agent roles to 'operator' (compatible with existing CHECK constraint).
    # Some agents may be 'admin' (from a prior hotfix) or 'agent' (from migration 018).
    # DB-driven (every type='agent' user) so no agent slugs are hardcoded in core.
    conn.execute(
        "UPDATE users SET system_role = 'operator', updated_at = datetime('now','utc') "
        "WHERE type = 'agent' AND system_role != 'operator'"
    )

    # 2. Seed the deploy's configured self-improvement / consolidation agents.
    # Slugs come from settings.self_improvement_agents (deploy .env); OSS core
    # hardcodes no internal agent names. Idempotent; inert on prod (already ran).
    for slug in settings.self_improvement_agents:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, slug, display_name, type, system_role, "
            "avatar_color, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', 'operator', '#8B5CF6', datetime('now','utc'), datetime('now','utc'))",
            (f"usr_{slug}", slug, slug),
        )

    # 3. Add last_accessed_at to documents (spaced repetition tracking)
    if not _column_exists(conn, "documents", "last_accessed_at"):
        conn.execute("ALTER TABLE documents ADD COLUMN last_accessed_at TEXT")
        logger.info("Migration 049: added documents.last_accessed_at")

    # 4. Add status + consolidated_from to learnings (draft lifecycle + anti-cycle)
    if not _column_exists(conn, "learnings", "status"):
        conn.execute(
            "ALTER TABLE learnings ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
        logger.info("Migration 049: added learnings.status")
    if not _column_exists(conn, "learnings", "consolidated_from"):
        conn.execute("ALTER TABLE learnings ADD COLUMN consolidated_from TEXT")
        logger.info("Migration 049: added learnings.consolidated_from")

    conn.commit()
    logger.info(
        "Migration 049: agent roles normalized + learnings schema + access tracking"
    )


def _backfill_inbox_status_from_treatment(conn: sqlite3.Connection) -> None:
    """Migration 058 post-hook: backfill inbox_items.status from treatment.

    Idempotent: only updates rows still at 'received' status.
    """
    cursor = conn.execute(
        """
        UPDATE inbox_items SET status = CASE treatment
            WHEN 'read'      THEN 'unread'
            WHEN 'save'      THEN 'saved'
            WHEN 'read_save' THEN 'unread'
            WHEN 'ignore'    THEN 'auto_ignored'
            ELSE 'unread'
        END
        WHERE status = 'received'
        """
    )
    conn.commit()
    logger.info(
        "Migration 058: backfilled %d inbox_items status from treatment",
        cursor.rowcount,
    )


def _add_deep_research_column(conn: sqlite3.Connection) -> None:
    """Migration 059 post-hook: add deep_research column to inbox_items if missing."""
    if not _column_exists(conn, "inbox_items", "deep_research"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN deep_research TEXT")
        conn.commit()
        logger.info("Migration 059: added inbox_items.deep_research column")


def _cleanup_generic_source_scores(conn: sqlite3.Connection) -> None:
    """Remove generic source score entries (rss-marvisx, gmail-marvisx) that are no longer useful."""
    cursor = conn.execute(
        "DELETE FROM source_scores WHERE source_key IN ('rss-marvisx', 'gmail-marvisx')"
    )
    conn.commit()
    if cursor.rowcount > 0:
        logger.info("Cleaned up %d generic source_scores entries", cursor.rowcount)


def _add_sent_in_newsletter_column(conn: sqlite3.Connection) -> None:
    """Migration 060 post-hook: add sent_in_newsletter column to inbox_items if missing."""
    if not _column_exists(conn, "inbox_items", "sent_in_newsletter"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN sent_in_newsletter TEXT")
        conn.commit()
        logger.info("Migration 060: added inbox_items.sent_in_newsletter column")


def _migration_061_backfill_sources(conn: sqlite3.Connection) -> None:
    """Migration 061 post-hook: backfill inbox_sources from distinct inbox_items.source.

    Normalizes source_key the SAME way as _update_source_score in inbox_triage:
    - URLs -> parsed netloc with optional www. prefix removed
    - non-URLs -> lowercase trimmed raw string

    Idempotent: INSERT OR IGNORE on the unique (workspace_id, source_key) index.
    Collisions (two raw sources that normalize to the same key) are logged but
    do not fail the migration.
    """
    from urllib.parse import urlparse

    if not _column_exists(conn, "inbox_items", "source"):
        logger.info("Migration 061: inbox_items.source missing, skipping backfill")
        return

    cursor = conn.execute(
        "SELECT DISTINCT source, COALESCE(workspace_id, 'ws_default') AS ws "
        "FROM inbox_items WHERE source IS NOT NULL AND source != ''"
    )
    rows = cursor.fetchall()

    seen_keys: set[tuple[str, str]] = set()
    collisions = 0
    inserted = 0

    for row in rows:
        raw_source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        ws = row["ws"] if isinstance(row, sqlite3.Row) else row[1]
        if not raw_source:
            continue

        source_key = raw_source
        try:
            parsed = urlparse(raw_source)
            if parsed.netloc:
                source_key = parsed.netloc.removeprefix("www.").lower()
            else:
                source_key = raw_source.strip().lower()
        except Exception:  # noqa: BLE001 - defensive, never fail migration
            source_key = raw_source.strip().lower()

        if not source_key:
            continue

        key_tuple = (ws, source_key)
        if key_tuple in seen_keys:
            collisions += 1
            continue
        seen_keys.add(key_tuple)

        result = conn.execute(
            "INSERT OR IGNORE INTO inbox_sources "
            "(id, name, source_key, source_type, active, workspace_id) "
            "VALUES (?, ?, ?, 'legacy', 1, ?)",
            (str(uuid_mod.uuid4()), raw_source[:200], source_key, ws),
        )
        if result.rowcount > 0:
            inserted += 1

    conn.commit()
    logger.info(
        "Migration 061: backfilled inbox_sources (inserted=%d, collisions=%d, total_distinct=%d)",
        inserted,
        collisions,
        len(rows),
    )


def _migration_062_backfill_from_urls(conn: sqlite3.Connection) -> None:
    """Migration 062 post-hook: re-backfill inbox_sources from URL domains.

    Migration 061 populated inbox_sources from inbox_items.source, but in
    production that column holds generic strings ("rss-marvisx", "gmail", ...)
    while the real article domain lives in inbox_items.url. The Sources
    Dashboard joins inbox_sources against source_scores, and source_scores
    is keyed by URL domain (see _update_source_score in inbox_triage), so the
    061 entries never matched any score row and all metrics rendered as zero.

    This hook extracts the real domain from DISTINCT inbox_items.url rows
    using the same urlparse + removeprefix("www.") + lowercase normalization
    that _update_source_score uses (modulo the explicit lowercase, which this
    hook applies defensively so case differences never break the JOIN).
    The legacy 061 rows are soft-deleted in the SQL portion of this migration
    (source_type='legacy', active=0) and left in place for audit history.

    Idempotent via the UNIQUE (workspace_id, source_key) index on
    inbox_sources; re-runs are safe and only log zero insertions.
    """
    from urllib.parse import urlparse

    if not _column_exists(conn, "inbox_items", "url"):
        logger.info("Migration 062: inbox_items.url missing, skipping backfill")
        return

    cursor = conn.execute(
        "SELECT DISTINCT url, COALESCE(workspace_id, 'ws_default') AS ws "
        "FROM inbox_items "
        "WHERE url IS NOT NULL AND url != ''"
    )
    rows = cursor.fetchall()

    seen: set[tuple[str, str]] = set()
    inserted = 0
    skipped = 0

    for row in rows:
        url = row["url"] if isinstance(row, sqlite3.Row) else row[0]
        ws = row["ws"] if isinstance(row, sqlite3.Row) else row[1]
        if not url:
            continue

        try:
            parsed = urlparse(url)
            netloc = (parsed.netloc or "").removeprefix("www.").lower()
        except Exception:  # noqa: BLE001 - defensive, never fail migration
            netloc = ""

        if not netloc:
            skipped += 1
            continue

        key = (ws, netloc)
        if key in seen:
            continue
        seen.add(key)

        result = conn.execute(
            "INSERT OR IGNORE INTO inbox_sources "
            "(id, name, source_key, source_type, active, workspace_id) "
            "VALUES (?, ?, ?, 'rss', 1, ?)",
            (str(uuid_mod.uuid4()), netloc, netloc, ws),
        )
        if result.rowcount > 0:
            inserted += 1

    conn.commit()
    logger.info(
        "Migration 062: backfilled inbox_sources from URL domains "
        "(inserted=%d, skipped_no_netloc=%d, distinct_urls=%d)",
        inserted,
        skipped,
        len(rows),
    )


def _promote_llm_costs_columns(conn: sqlite3.Connection) -> None:
    """Migration 102 post-hook: ALTER llm_costs to add tier_logical / fallback_used / litellm_request_id.

    The SQL migration 102 only does CREATE TABLE IF NOT EXISTS (idempotent,
    fresh DBs get the full new schema directly). Production DBs already had
    the table lazy-created by inbox_llm_classifier with the old 8-column
    schema; this hook adds the 3 new columns guarded by _column_exists().

    Why a hook instead of pure SQL: SQLite has no `ALTER TABLE ... ADD COLUMN
    IF NOT EXISTS`, and bare ALTER inside an executescript() raises
    "duplicate column name" on fresh DBs (where CREATE already provisioned
    them) which would abort the whole script and leave the migration in an
    inconsistent state. The hook runs Python-side after CREATE so we can
    branch safely.
    """
    if not _column_exists(conn, "llm_costs", "tier_logical"):
        conn.execute("ALTER TABLE llm_costs ADD COLUMN tier_logical TEXT")
        logger.info("Migration 102: added llm_costs.tier_logical")
    if not _column_exists(conn, "llm_costs", "fallback_used"):
        conn.execute(
            "ALTER TABLE llm_costs ADD COLUMN fallback_used INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("Migration 102: added llm_costs.fallback_used")
    if not _column_exists(conn, "llm_costs", "litellm_request_id"):
        conn.execute("ALTER TABLE llm_costs ADD COLUMN litellm_request_id TEXT")
        logger.info("Migration 102: added llm_costs.litellm_request_id")
    conn.commit()


def _migration_135_graph_edges_provider(conn: sqlite3.Connection) -> None:
    """Add provider column/index for KG edges without retroactive backfill."""
    if not _column_exists(conn, "graph_edges", "provider"):
        conn.execute("ALTER TABLE graph_edges ADD COLUMN provider TEXT")
        logger.info("Migration 135: added graph_edges.provider")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_provider "
        "ON graph_edges(provider, relation)"
    )
    conn.commit()


def _add_session_theme_mode_column(conn: sqlite3.Connection) -> None:
    """Ensure sessions_meta.theme_mode exists for existing databases."""
    if not _column_exists(conn, "sessions_meta", "theme_mode"):
        conn.execute(
            "ALTER TABLE sessions_meta ADD COLUMN theme_mode TEXT DEFAULT NULL"
        )
        logger.info("Added sessions_meta.theme_mode")
    conn.commit()


async def cleanup_expired_tickets(db: aiosqlite.Connection) -> int:
    """Remove expired WS tickets. Returns count deleted."""
    cursor = await db.execute(
        "DELETE FROM ws_tickets WHERE expires_at < datetime('now')"
    )
    await db.commit()
    return cursor.rowcount


async def cleanup_expired_blacklist(db: aiosqlite.Connection) -> int:
    """Remove expired blacklist entries. Returns count deleted."""
    cursor = await db.execute(
        "DELETE FROM token_blacklist WHERE expires_at < datetime('now')"
    )
    await db.commit()
    return cursor.rowcount
