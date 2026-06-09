"""Filesystem watcher for Universal Ingestion phase 1."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import signal
import sys
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from watchfiles import Change, DefaultFilter, awatch

from core.api.db import acquire_write_db, close_pool, init_pool
from core.api.services.ingest.events import broadcast_ingest_changed
from core.api.services.ingest.parser_router import detect_mime, parse_pending
from core.api.services.ingest.skip_log import log_skip

try:
    import sdnotify
except ImportError:  # pragma: no cover - optional in dev/test
    sdnotify = None  # type: ignore[assignment]

logger = logging.getLogger("ingest_watcher")

PROJECTS_ROOT = Path("/data/projects")
ACCEPTED_SUFFIXES = {
    ".aac",
    ".docx",
    ".flac",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".markdown",
    ".md",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".oga",
    ".ogg",
    ".opus",
    ".pdf",
    ".png",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xlsx",
}
REJECTED_SUFFIXES = (".tmp", ".swp", ".bak", "~")
DEBOUNCE_MS = 1_000
HEARTBEAT_SEC = 20
DEFAULT_SCAN_MAX_FILES = 100
FILE_STABLE_INTERVAL_SEC = 0.35
FILE_STABLE_MIN_CHECKS = 2
FILE_STABLE_MAX_CHECKS = 8
PATH_DEDUP_STATUSES = (
    "queued",
    "parser_waiting",
    "parsing",
    "classified",
    "awaiting_triage",
    "approved",
    "inserted",
)
TERMINAL_RACE_PROMOTE_STATUSES = {"queued", "parser_waiting"}


class _IngestFilter(DefaultFilter):
    def __call__(self, change: Change, path: str) -> bool:
        if not super().__call__(change, path):
            return False
        p = Path(path)
        name = p.name
        if name.startswith(("#", "~", ".")):
            return False
        if name.endswith(REJECTED_SUFFIXES):
            return False
        if p.suffix.lower() not in ACCEPTED_SUFFIXES:
            return False
        return "input" in p.parts


def _accepted_file(path: Path) -> bool:
    name = path.name
    if name.startswith(("#", "~", ".")):
        return False
    if name.endswith(REJECTED_SUFFIXES):
        return False
    return path.is_file() and path.suffix.lower() in ACCEPTED_SUFFIXES


def _runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    path = base / "pir-ingest-watcher"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _acquire_lock() -> None:
    lock_file = _runtime_dir() / "lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("another ingest watcher is already running")
        sys.exit(1)


def _project_and_path(path: Path, projects_root: Path) -> tuple[str, Path] | None:
    resolved = path.resolve()
    root = projects_root.resolve()
    if not resolved.is_relative_to(root):
        return None
    rel = resolved.relative_to(root)
    if len(rel.parts) < 3 or rel.parts[1] != "input":
        return None
    return rel.parts[0], resolved


def _path_is_in_input_tree(path: Path, projects_root: Path) -> bool:
    resolved = path.resolve()
    root = projects_root.resolve()
    if not resolved.is_relative_to(root):
        return False
    rel = resolved.relative_to(root)
    return len(rel.parts) >= 2 and rel.parts[1] == "input"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _wait_for_stable_file(
    path: Path,
    *,
    interval_sec: float = FILE_STABLE_INTERVAL_SEC,
    min_stable_checks: int = FILE_STABLE_MIN_CHECKS,
    max_checks: int = FILE_STABLE_MAX_CHECKS,
) -> bool:
    """Return after size+mtime stay unchanged long enough to hash safely."""

    last_signature: tuple[int, int] | None = None
    stable_checks = 0
    for _ in range(max_checks):
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        signature = (stat.st_size, stat.st_mtime_ns)
        if signature == last_signature:
            stable_checks += 1
            if stable_checks >= min_stable_checks:
                return True
        else:
            last_signature = signature
            stable_checks = 0
        await asyncio.sleep(interval_sec)
    logger.warning("file did not fully stabilize before enqueue: %s", path)
    return path.exists() and path.is_file()


EnqueueOutcome = Literal["fresh", "reactivated", "dedup", "invalid"]


async def enqueue_file(
    path: Path,
    *,
    projects_root: Path = PROJECTS_ROOT,
    source_kind: str = "file_drop",
    api_key_id: str | None = None,
    source: str | None = None,
    ingest_policy: str | None = None,
    metadata: dict | None = None,
) -> tuple[str | None, EnqueueOutcome]:
    """Enqueue a file into ingest_pending or audit-skip on collision.

    Returns ``(ingest_id, outcome)`` where outcome is one of:
      - ``fresh``: new row inserted, parse_pending scheduled
      - ``reactivated``: existing rejected row re-queued, parse_pending scheduled
      - ``dedup``: existing non-rejected row, no work scheduled,
        ingest_skipped audit row written
      - ``invalid``: path outside projects_root or not a file (id is None)

    ``api_key_id`` / ``source`` / ``ingest_policy`` are the M1 CAPTURE ingress
    governance columns; owner-surface callers (the FS watcher, multipart upload)
    leave them None.
    """
    if source_kind not in {
        "file_drop",
        "manual_upload",
        "api_upload",
        "terminal_upload",
        "api_ingress",
    }:
        raise ValueError(f"Unsupported ingest source kind: {source_kind}")
    parsed = _project_and_path(path, projects_root)
    if parsed is None:
        return None, "invalid"
    project_slug, resolved = parsed
    if not resolved.exists() or not resolved.is_file():
        return None, "invalid"

    ingest_id = str(uuid.uuid4())
    digest = _sha256(resolved)
    stat = resolved.stat()
    mime_type = detect_mime(resolved)
    inserted_id: str | None = None
    reactivated = False
    existing_status: str | None = None
    dedup_reason = "dedup_sha256"
    async with acquire_write_db() as db:
        placeholders = ", ".join("?" for _ in PATH_DEDUP_STATUSES)
        async with db.execute(
            """
            SELECT id, status, source_kind, file_path, sha256
              FROM ingest_pending
             WHERE project_slug = ?
               AND file_path = ?
               AND status IN (""" + placeholders + """)
             ORDER BY created_at ASC
             LIMIT 1
            """,
            (project_slug, str(resolved), *PATH_DEDUP_STATUSES),
        ) as cursor:
            row = await cursor.fetchone()

        if row is not None:
            existing_id = row["id"]
            existing_status = row["status"]
            dedup_reason = "dedup_path"
            if (
                source_kind == "terminal_upload"
                and row["source_kind"] == "file_drop"
                and existing_status in TERMINAL_RACE_PROMOTE_STATUSES
            ):
                await db.execute(
                    """
                    UPDATE ingest_pending
                       SET source_kind = 'terminal_upload',
                           sha256 = ?,
                           file_size_bytes = ?,
                           mime_type = ?,
                           updated_at = datetime('now')
                     WHERE id = ?
                    """,
                    (digest, stat.st_size, mime_type, existing_id),
                )
        else:
            await db.execute(
                """
                INSERT OR IGNORE INTO ingest_pending
                    (id, file_path, sha256, project_slug, source_kind, mime_type,
                     file_size_bytes, status, created_at, updated_at,
                     api_key_id, source, ingest_policy, ingress_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', datetime('now'), datetime('now'),
                        ?, ?, ?, ?)
                """,
                (
                    ingest_id,
                    str(resolved),
                    digest,
                    project_slug,
                    source_kind,
                    mime_type,
                    stat.st_size,
                    api_key_id,
                    source,
                    ingest_policy,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                ),
            )
            async with db.execute(
                """
                SELECT id, status, source_kind, file_path
                  FROM ingest_pending
                 WHERE sha256 = ?
                   AND project_slug = ?
                """,
                (digest, project_slug),
            ) as cursor:
                row = await cursor.fetchone()

        existing_id = row["id"] if row else None
        existing_status = existing_status or (row["status"] if row else None)
        # Re-upload of a rejected file: reset row to queued so parse_pending picks
        # it up again (UNIQUE(sha256, project_slug) blocks a fresh INSERT).
        if existing_id is not None and existing_id != ingest_id and existing_status == "rejected":
            await db.execute(
                """
                UPDATE ingest_pending
                   SET status = 'queued',
                       error_message = NULL,
                       triage_decision_id = NULL,
                       classification_json = NULL,
                       parser_used = NULL,
                       extracted_text = NULL,
                       structure_json = NULL,
                       target_folder = NULL,
                       target_filename = NULL,
                       file_path = ?,
                       file_size_bytes = ?,
                       mime_type = ?,
                       source_kind = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (str(resolved), stat.st_size, mime_type, source_kind, existing_id),
            )
            reactivated = True
        elif (
            existing_id is not None
            and existing_id != ingest_id
            and source_kind == "terminal_upload"
            and row["source_kind"] == "file_drop"
            and Path(row["file_path"]).resolve() == resolved
            and existing_status in {"queued", "parser_waiting"}
        ):
            await db.execute(
                """
                UPDATE ingest_pending
                   SET source_kind = 'terminal_upload',
                       sha256 = ?,
                       file_size_bytes = ?,
                       mime_type = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (digest, stat.st_size, mime_type, existing_id),
            )
        await db.commit()
        inserted_id = existing_id

    fresh = inserted_id == ingest_id
    outcome: EnqueueOutcome
    if fresh:
        outcome = "fresh"
    elif reactivated:
        outcome = "reactivated"
    else:
        outcome = "dedup"

    if fresh or reactivated:
        await broadcast_ingest_changed(
            "queued",
            ingest_id=inserted_id,
            project_slug=project_slug,
            status="queued",
        )
        asyncio.create_task(parse_pending(inserted_id))
    elif inserted_id is not None:
        # UX-6: silent dedup against an existing non-rejected row. Log to
        # ingest_skipped so the frontend "Ignorati" sidebar surfaces it
        # instead of letting the upload appear successful but invisible.
        async with acquire_write_db() as db:
            await log_skip(
                db,
                file_path_attempted=str(resolved),
                project_slug=project_slug,
                reason="dedup_sha256",
                sha256=digest,
                existing_ingest_id=inserted_id,
                error_message=f"already in pipeline by {dedup_reason} (status={existing_status})",
            )
    return inserted_id, outcome


async def _heartbeat_loop(notifier) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        if notifier is not None:
            notifier.notify("WATCHDOG=1")


async def _watch_loop(projects_root: Path, stop_event: asyncio.Event) -> None:
    roots = _watch_roots(projects_root)
    if not roots:
        logger.warning("no ingest input directories found under %s", projects_root)
        await stop_event.wait()
        return

    logger.info(
        "watching %s ingest input director%s",
        len(roots),
        "y" if len(roots) == 1 else "ies",
    )
    async for changes in awatch(
        *(str(root) for root in roots),
        watch_filter=_IngestFilter(),
        debounce=DEBOUNCE_MS,
        step=100,
        recursive=True,
        ignore_permission_denied=True,
        stop_event=stop_event,
    ):
        for change, path_str in changes:
            if change == Change.deleted:
                continue
            try:
                path = Path(path_str)
                if await _wait_for_stable_file(path):
                    await enqueue_file(path, projects_root=projects_root)
            except Exception:
                logger.exception("failed to enqueue changed file: %s", path_str)


def _watch_roots(projects_root: Path) -> list[Path]:
    """Return only existing <project>/input directories.

    Watching all of /data/projects lets malformed filenames outside ingest scope
    crash watchfiles before Python filters run. The watcher only needs project
    input landing zones.
    """
    if not projects_root.exists():
        return []
    try:
        entries = sorted(projects_root.iterdir())
    except OSError:
        logger.exception("failed to list projects root: %s", projects_root)
        return []

    roots: list[Path] = []
    for project_dir in entries:
        input_dir = project_dir / "input"
        if input_dir.is_dir():
            roots.append(input_dir.resolve())
    return roots


def _validate_project_slug(slug: str) -> str:
    stripped = slug.strip()
    if not stripped:
        raise ValueError("scan project cannot be empty")
    candidate = Path(stripped)
    if candidate.is_absolute() or len(candidate.parts) != 1 or stripped in {".", ".."}:
        raise ValueError(f"scan project must be a slug, got {slug!r}")
    return stripped


def _scan_roots(
    projects_root: Path,
    *,
    project_slugs: Iterable[str],
    scan_paths: Iterable[Path],
) -> list[Path]:
    roots: list[Path] = []
    for slug in project_slugs:
        input_dir = projects_root / _validate_project_slug(slug) / "input"
        if not input_dir.exists():
            raise FileNotFoundError(f"scan project input dir not found: {input_dir}")
        if not input_dir.is_dir():
            raise NotADirectoryError(f"scan project input path is not a dir: {input_dir}")
        roots.append(input_dir.resolve())

    for raw_path in scan_paths:
        path = raw_path.expanduser()
        if not path.is_absolute():
            path = projects_root / path
        if not path.exists():
            raise FileNotFoundError(f"scan path not found: {path}")
        if not _path_is_in_input_tree(path, projects_root):
            raise ValueError(f"scan path must be under <project>/input: {path}")
        roots.append(path.resolve())

    if not roots:
        raise ValueError("bounded scan requires --scan-project or --scan-path")
    return roots


async def _scan_existing(
    projects_root: Path,
    *,
    project_slugs: Iterable[str] = (),
    scan_paths: Iterable[Path] = (),
    max_files: int = DEFAULT_SCAN_MAX_FILES,
) -> int:
    if max_files < 1:
        raise ValueError("scan max files must be >= 1")

    attempted = 0
    seen: set[Path] = set()
    roots = _scan_roots(projects_root, project_slugs=project_slugs, scan_paths=scan_paths)
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen or not _accepted_file(resolved):
                continue
            seen.add(resolved)
            try:
                await enqueue_file(resolved, projects_root=projects_root)
            except Exception:
                logger.exception("failed to enqueue existing file: %s", resolved)
            attempted += 1
            if attempted >= max_files:
                logger.warning("bounded startup scan stopped at max_files=%s", max_files)
                return attempted
    return attempted


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Marvis file ingest watcher")
    parser.add_argument("--projects-root", default=str(PROJECTS_ROOT))
    parser.add_argument(
        "--scan-on-start",
        action="store_true",
        help=(
            "Run a bounded recovery scan before watching. Requires "
            "--scan-project or --scan-path."
        ),
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Run the bounded scan and exit without starting the filesystem watcher.",
    )
    parser.add_argument(
        "--scan-project",
        action="append",
        default=[],
        metavar="SLUG",
        help="Project slug whose input/ directory should be scanned. Repeatable.",
    )
    parser.add_argument(
        "--scan-path",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="File or directory under <project>/input to scan. Repeatable.",
    )
    parser.add_argument(
        "--scan-max-files",
        default=int(os.environ.get("INGEST_WATCHER_SCAN_MAX_FILES", DEFAULT_SCAN_MAX_FILES)),
        type=_positive_int,
        help="Maximum files a startup/recovery scan may enqueue.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("INGEST_WATCHER_LOG_LEVEL", "INFO"),
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.scan_only and not args.scan_on_start:
        parser.error("--scan-only requires --scan-on-start")
    if args.scan_on_start and not (args.scan_project or args.scan_path):
        parser.error("--scan-on-start requires --scan-project or --scan-path")
    return args


async def _async_main(args: argparse.Namespace) -> int:
    _acquire_lock()
    projects_root = Path(args.projects_root or PROJECTS_ROOT)
    if not projects_root.exists():
        logger.error("projects root does not exist: %s", projects_root)
        return 2

    await init_pool()
    notifier = sdnotify.SystemdNotifier() if sdnotify else None
    if notifier is not None:
        notifier.notify(f"READY=1\nSTATUS=watching {projects_root}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(sig: int) -> None:
        logger.info("received signal %s; stopping", sig)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)

    try:
        if args.scan_on_start:
            scanned = await _scan_existing(
                projects_root,
                project_slugs=args.scan_project,
                scan_paths=args.scan_path,
                max_files=args.scan_max_files,
            )
            logger.info("bounded startup scan attempted %s file(s)", scanned)
            if args.scan_only:
                return 0
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_watch_loop(projects_root, stop_event), name="watch_loop")
            tg.create_task(_heartbeat_loop(notifier), name="heartbeat_loop")
            await stop_event.wait()
            for task in tg._tasks:
                task.cancel()
    except* asyncio.CancelledError:
        pass
    finally:
        if notifier is not None:
            notifier.notify("STOPPING=1")
        await close_pool()
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
