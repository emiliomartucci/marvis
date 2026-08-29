"""Atomic project lifecycle and common Cloud/F change control.

This module is deliberately transport-free.  HTTP and MCP call the same
functions with a :class:`CallerContext`; SQLite owns compare-and-set state and
the filesystem lock serializes project.yaml plus every guarded file writer.

An archive is a persisted saga:

``prepared -> filesystem_applied -> completed``

The ``prepared`` commit fences all later writers before project.yaml changes.
Retrying the same idempotency key resumes the operation; a different payload is
rejected.  A crash therefore causes safe containment, never an unfenced partial
archive.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from weakref import WeakKeyDictionary

import aiosqlite
import yaml

from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
    resolve_approval_authority,
)
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ServiceUnavailableError,
    ValidationError,
)
from core.platform.locking import LockUnavailableError, exclusive_file_lock

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPERATION_KIND_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_LIFECYCLES = {"idea", "planning", "active", "maintenance", "archived"}
_EMPTY_ACTIVE_OPERATIONS_DIGEST = hashlib.sha256(b"[]").hexdigest()
_ASYNC_MUTATION_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = WeakKeyDictionary()


@dataclass(frozen=True)
class ProjectLifecycleSnapshot:
    workspace_id: str
    project_slug: str
    project_id: str
    lifecycle: str
    project_digest: str | None
    writer_watermark: int
    selector_watermark: str
    transition_operation_id: str | None
    archived_at: str | None
    archived_by: str | None
    archive_approval_id: str | None


@dataclass(frozen=True)
class CloudFControlSnapshot:
    workspace_id: str
    change_epoch: int
    readiness_state: str
    readiness_subtype: str | None
    lease_generation: int
    lease_operation_id: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    active_operations: tuple[dict[str, Any], ...]
    active_operations_digest: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_slug(project_slug: str) -> str:
    slug = (project_slug or "").strip()
    if not _SLUG_RE.fullmatch(slug):
        raise ValidationError(
            code="invalid_project_slug",
            message="Invalid project slug",
        )
    return slug


def _validate_digest(name: str, value: str) -> str:
    normalized = (value or "").strip().lower()
    if not _HEX_64_RE.fullmatch(normalized):
        raise ValidationError(
            code="invalid_digest",
            message=f"{name} must be a lowercase SHA-256 digest",
        )
    return normalized


def _validate_token(name: str, value: str, pattern: re.Pattern[str]) -> str:
    normalized = (value or "").strip()
    if not pattern.fullmatch(normalized):
        raise ValidationError(
            code=f"invalid_{name}",
            message=f"Invalid {name.replace('_', ' ')}",
        )
    return normalized


def _validate_idempotency_key(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 200:
        raise ValidationError(
            code="invalid_idempotency_key",
            message="Idempotency key must contain 1 to 200 characters",
        )
    return normalized


def _future_timestamp(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(
            code=f"{name}_timezone_required",
            message=f"{name.replace('_', ' ').title()} must include a timezone",
        )
    normalized = value.astimezone(timezone.utc)
    if normalized <= _utc_now():
        raise ValidationError(
            code=f"{name}_expired",
            message=f"{name.replace('_', ' ').title()} must be in the future",
        )
    return normalized


def _canonical_digest(payload: Mapping[str, Any] | list[Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _row_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def _project_root(projects_root: Path | None = None) -> Path:
    if projects_root is not None:
        return Path(projects_root).expanduser().resolve()
    from core.api.use_cases.projects import data_project_dir

    return data_project_dir().resolve()


def project_directory(
    project_slug: str,
    *,
    projects_root: Path | None = None,
) -> Path:
    slug = _validate_slug(project_slug)
    root = _project_root(projects_root)
    lexical_candidate = root / slug
    if lexical_candidate.is_symlink():
        raise ValidationError(
            code="project_symlink_denied",
            message="Project directory cannot be a symbolic link",
        )
    candidate = lexical_candidate.resolve()
    if candidate.parent != root:
        raise ValidationError(
            code="invalid_project_path",
            message="Project path escapes the projects root",
        )
    return candidate


@contextmanager
def project_mutation_guard(
    *,
    projects_root: Path | None = None,
) -> Iterator[None]:
    """Cross-process lock shared by create, archive, and file writers.

    The filename intentionally remains ``.project-create.lock`` so a mixed
    old/new process rollout still serializes against the pre-U10 creator.
    """
    root = _project_root(projects_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceUnavailableError(
            code="project_mutation_lock_unavailable",
            message="Project mutation lock directory is unavailable",
        ) from exc
    lock_path = root / ".project-create.lock"
    with ExitStack() as stack:
        try:
            stack.enter_context(
                exclusive_file_lock(lock_path, mode=0o600, nofollow=True)
            )
        except LockUnavailableError as exc:
            raise ServiceUnavailableError(
                code="project_mutation_lock_unavailable",
                message="Project mutation lock is unavailable",
            ) from exc
        yield


@asynccontextmanager
async def async_project_mutation_guard(
    *,
    projects_root: Path | None = None,
):
    """Event-loop-safe wrapper around the cross-process filesystem lock."""
    root = _project_root(projects_root)
    loop = asyncio.get_running_loop()
    locks = _ASYNC_MUTATION_LOCKS.setdefault(loop, {})
    lock = locks.setdefault(str(root), asyncio.Lock())
    async with lock:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ServiceUnavailableError(
                code="project_mutation_lock_unavailable",
                message="Project mutation lock directory is unavailable",
            ) from exc
        lock_path = root / ".project-create.lock"
        lock_context = exclusive_file_lock(
            lock_path,
            mode=0o600,
            nofollow=True,
            # FileLock stores ownership per thread by default on Windows, while
            # asyncio.to_thread may choose different workers for enter/exit.
            thread_local=False,
        )
        lock_acquired = False
        acquire_task = asyncio.create_task(
            asyncio.to_thread(lock_context.__enter__)
        )
        try:
            try:
                await asyncio.shield(acquire_task)
                lock_acquired = True
            except asyncio.CancelledError:
                # The worker continues after cancellation. Wait for acquisition
                # to finish, release immediately when it succeeded, and only
                # then propagate so a delayed lock can never leak.
                try:
                    await acquire_task
                    lock_acquired = True
                except LockUnavailableError:
                    pass
                raise
            except LockUnavailableError as exc:
                raise ServiceUnavailableError(
                    code="project_mutation_lock_unavailable",
                    message="Project mutation lock is unavailable",
                ) from exc
            yield
        finally:
            if lock_acquired:
                release_task = asyncio.create_task(
                    asyncio.to_thread(lock_context.__exit__, None, None, None)
                )
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError:
                    # A second cancellation must not let cleanup escape in the
                    # background while the task appears finished.
                    await release_task
                    raise


def _project_yaml_bytes(project_dir: Path) -> bytes:
    yaml_path = project_dir / "project.yaml"
    if not project_dir.is_dir() or not yaml_path.is_file() or yaml_path.is_symlink():
        raise NotFoundError(
            code="project_not_found",
            message="Project metadata was not found",
        )
    try:
        return yaml_path.read_bytes()
    except OSError as exc:
        raise ServiceUnavailableError(
            code="project_metadata_unavailable",
            message="Project metadata cannot be read",
        ) from exc


def project_digest(project_dir: Path) -> str:
    return hashlib.sha256(_project_yaml_bytes(project_dir)).hexdigest()


def _read_project_yaml(project_dir: Path) -> tuple[dict[str, Any], bytes]:
    raw = _project_yaml_bytes(project_dir)
    try:
        parsed = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ServiceError(
            code="project_metadata_invalid",
            message="project.yaml is not valid UTF-8 YAML",
        ) from exc
    if not isinstance(parsed, dict):
        raise ServiceError(
            code="project_metadata_invalid",
            message="project.yaml must contain a mapping",
        )
    return parsed, raw


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _render_project_yaml(data: Mapping[str, Any]) -> bytes:
    return yaml.safe_dump(
        dict(data),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).encode("utf-8")


def _atomic_write_project_yaml(project_dir: Path, data: Mapping[str, Any]) -> str:
    target = project_dir / "project.yaml"
    rendered = _render_project_yaml(data)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".project.yaml.",
        suffix=".tmp",
        dir=project_dir,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(project_dir)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return hashlib.sha256(rendered).hexdigest()


async def _fetch_lifecycle_row(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT workspace_id,project_slug,project_id,lifecycle,project_digest,"
        "writer_watermark,selector_watermark,transition_operation_id,"
        "archived_at,archived_by,archive_approval_id "
        "FROM project_lifecycle_state WHERE workspace_id=? AND project_slug=?",
        (workspace_id, project_slug),
    )
    return await cursor.fetchone()


def _lifecycle_snapshot(row: aiosqlite.Row) -> ProjectLifecycleSnapshot:
    return ProjectLifecycleSnapshot(
        workspace_id=str(row["workspace_id"]),
        project_slug=str(row["project_slug"]),
        project_id=str(row["project_id"]),
        lifecycle=str(row["lifecycle"]),
        project_digest=row["project_digest"],
        writer_watermark=int(row["writer_watermark"]),
        selector_watermark=str(row["selector_watermark"] or ""),
        transition_operation_id=row["transition_operation_id"],
        archived_at=row["archived_at"],
        archived_by=row["archived_by"],
        archive_approval_id=row["archive_approval_id"],
    )


async def ensure_project_lifecycle(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
    projects_root: Path | None = None,
) -> ProjectLifecycleSnapshot:
    """Register one existing project without changing an established state."""
    slug = _validate_slug(project_slug)
    project_dir = project_directory(slug, projects_root=projects_root)
    metadata, raw = _read_project_yaml(project_dir)
    lifecycle = str(metadata.get("lifecycle") or "active")
    if lifecycle not in _LIFECYCLES:
        raise ServiceError(
            code="project_lifecycle_invalid",
            message="project.yaml contains an unsupported lifecycle",
        )
    digest = hashlib.sha256(raw).hexdigest()
    project_id = "prj_" + uuid.uuid4().hex
    archived_at = _iso(_utc_now()) if lifecycle == "archived" else None
    await db.execute(
        "INSERT OR IGNORE INTO project_lifecycle_state "
        "(workspace_id,project_slug,project_id,lifecycle,project_digest,archived_at) "
        "VALUES (?,?,?,?,?,?)",
        (workspace_id, slug, project_id, lifecycle, digest, archived_at),
    )
    row = await _fetch_lifecycle_row(
        db,
        workspace_id=workspace_id,
        project_slug=slug,
    )
    if row is None:  # pragma: no cover - defensive schema corruption guard
        raise ServiceUnavailableError(
            code="project_lifecycle_unavailable",
            message="Project lifecycle state could not be registered",
        )
    return _lifecycle_snapshot(row)


async def read_project_lifecycle(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
) -> ProjectLifecycleSnapshot:
    row = await _fetch_lifecycle_row(
        db,
        workspace_id=workspace_id,
        project_slug=_validate_slug(project_slug),
    )
    if row is None:
        raise NotFoundError(
            code="project_lifecycle_not_registered",
            message="Project lifecycle state is not registered",
        )
    return _lifecycle_snapshot(row)


async def record_project_write(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
    writer_kind: str,
    actor: str | None,
    resource_ref: str | None = None,
    operation_id: str | None = None,
    projects_root: Path | None = None,
) -> int:
    """Journal and fence a filesystem mutation inside its caller transaction."""
    slug = _validate_slug(project_slug)
    existing = await _fetch_lifecycle_row(
        db,
        workspace_id=workspace_id,
        project_slug=slug,
    )
    if existing is None:
        await ensure_project_lifecycle(
            db,
            workspace_id=workspace_id,
            project_slug=slug,
            projects_root=projects_root,
        )
    try:
        cursor = await db.execute(
            "INSERT INTO project_write_events "
            "(workspace_id,project_slug,writer_kind,operation_id,actor,resource_ref) "
            "VALUES (?,?,?,?,?,?)",
            (
                workspace_id,
                slug,
                writer_kind,
                operation_id,
                actor,
                resource_ref,
            ),
        )
    except aiosqlite.IntegrityError as exc:
        if "project_not_writable" in str(exc):
            raise ConflictError(
                code="project_not_writable",
                message="Project is archived or a lifecycle transition is active",
            ) from exc
        raise
    return int(cursor.lastrowid or 0)


@asynccontextmanager
async def guarded_project_file_write(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    writer_kind: str,
    resource_ref: str | None = None,
    operation_id: str | None = None,
    projects_root: Path | None = None,
):
    """Journal one filesystem mutation before exposing its side effect.

    The event is committed while the cross-process project lock is held.  A
    later filesystem error can conservatively invalidate an archive approval;
    an unjournaled late write can never race the archive transition.
    """
    async with async_project_mutation_guard(projects_root=projects_root):
        await record_project_write(
            db,
            workspace_id=require_workspace_ctx(ctx),
            project_slug=project_slug,
            writer_kind=writer_kind,
            actor=ctx.user_id or ctx.username,
            resource_ref=resource_ref,
            operation_id=operation_id,
            projects_root=projects_root,
        )
        await db.commit()
        yield


@asynccontextmanager
async def isolated_project_file_write(
    ctx: CallerContext,
    *,
    project_slug: str,
    writer_kind: str,
    resource_ref: str | None = None,
    operation_id: str | None = None,
    projects_root: Path | None = None,
):
    """Journal an isolated filesystem mutation, then hold only its project lock.

    The application writer lock is always acquired before the project lock,
    matching request handlers that receive ``get_write_db`` before entering a
    lifecycle service.  The writer event is committed before the side effect;
    the global DB writer is then released while the cross-process project lock
    remains held.  This preserves the single lock order without serializing
    slow uploads/copies behind the database writer.  Callers must perform any
    later DB metadata update only after this context exits.
    """
    from core.api.db import acquire_write_db

    root = _project_root(projects_root)
    project_guard = async_project_mutation_guard(projects_root=root)
    project_lock_acquired = False
    try:
        async with acquire_write_db(label=f"project-file:{writer_kind}") as db:
            await project_guard.__aenter__()
            project_lock_acquired = True
            await record_project_write(
                db,
                workspace_id=require_workspace_ctx(ctx),
                project_slug=project_slug,
                writer_kind=writer_kind,
                actor=ctx.user_id or ctx.username,
                resource_ref=resource_ref,
                operation_id=operation_id,
                projects_root=root,
            )
            await db.commit()
        yield
    finally:
        if project_lock_acquired:
            await project_guard.__aexit__(None, None, None)


async def assert_project_writable(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
) -> ProjectLifecycleSnapshot:
    state = await read_project_lifecycle(
        db,
        workspace_id=workspace_id,
        project_slug=project_slug,
    )
    if state.lifecycle == "archived" or state.transition_operation_id is not None:
        raise ConflictError(
            code="project_not_writable",
            message="Project is archived or a lifecycle transition is active",
        )
    return state


async def _active_operations(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
) -> tuple[tuple[dict[str, Any], ...], str]:
    cursor = await db.execute(
        "SELECT operation_id,operation_kind,actor,base_epoch,lease_generation,started_at "
        "FROM cloud_f_active_operations WHERE workspace_id=? ORDER BY operation_id",
        (workspace_id,),
    )
    rows = tuple(_row_dict(row) or {} for row in await cursor.fetchall())
    return rows, _canonical_digest(list(rows))


async def ensure_cloud_f_control(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
) -> CloudFControlSnapshot:
    await db.execute(
        "INSERT OR IGNORE INTO cloud_f_control(workspace_id) VALUES (?)",
        (workspace_id,),
    )
    return await read_cloud_f_control(db, workspace_id=workspace_id)


async def read_cloud_f_control(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
) -> CloudFControlSnapshot:
    cursor = await db.execute(
        "SELECT workspace_id,change_epoch,readiness_state,readiness_subtype,"
        "lease_generation,lease_operation_id,lease_owner,lease_expires_at "
        "FROM cloud_f_control WHERE workspace_id=?",
        (workspace_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise NotFoundError(
            code="cloud_f_control_not_initialized",
            message="Cloud/F control is not initialized",
        )
    operations, digest = await _active_operations(db, workspace_id=workspace_id)
    return CloudFControlSnapshot(
        workspace_id=str(row["workspace_id"]),
        change_epoch=int(row["change_epoch"]),
        readiness_state=str(row["readiness_state"]),
        readiness_subtype=row["readiness_subtype"],
        lease_generation=int(row["lease_generation"]),
        lease_operation_id=row["lease_operation_id"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        active_operations=operations,
        active_operations_digest=digest,
    )


async def _cloud_f_change_operation_row(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM cloud_f_change_operations "
        "WHERE workspace_id=? AND operation_id=?",
        (workspace_id, operation_id),
    )
    return await cursor.fetchone()


def _cloud_f_result_snapshot(row: aiosqlite.Row) -> CloudFControlSnapshot:
    try:
        payload = json.loads(str(row["result_json"]))
        operations = tuple(dict(item) for item in payload["active_operations"])
        return CloudFControlSnapshot(
            workspace_id=str(payload["workspace_id"]),
            change_epoch=int(payload["change_epoch"]),
            readiness_state=str(payload["readiness_state"]),
            readiness_subtype=payload.get("readiness_subtype"),
            lease_generation=int(payload["lease_generation"]),
            lease_operation_id=payload.get("lease_operation_id"),
            lease_owner=payload.get("lease_owner"),
            lease_expires_at=payload.get("lease_expires_at"),
            active_operations=operations,
            active_operations_digest=str(payload["active_operations_digest"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ServiceUnavailableError(
            code="cloud_f_operation_corrupt",
            message="Cloud/F operation result journal is invalid",
        ) from exc


async def activate_cloud_f_control(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    subtype: str,
    expected_epoch: int,
) -> CloudFControlSnapshot:
    """One-time bootstrap/adoption primitive; U11 owns its live invocation."""
    require_role_ctx(ctx, "admin", "super_admin")
    authority = await resolve_approval_authority(ctx, db)
    if authority is None:
        raise AuthorizationError(
            code="approval_authority_required",
            message="Cloud/F readiness requires human or persisted delegated approval",
        )
    if subtype not in {"bootstrap_activation", "existing_live_adoption"}:
        raise ValidationError(
            code="invalid_readiness_subtype",
            message="Unsupported Cloud/F readiness subtype",
        )
    workspace_id = require_workspace_ctx(ctx)
    control = await ensure_cloud_f_control(db, workspace_id=workspace_id)
    activated_epoch = (
        expected_epoch + 1 if subtype == "bootstrap_activation" else expected_epoch
    )
    if control.readiness_state == "ready":
        if (
            control.readiness_subtype == subtype
            and control.change_epoch == activated_epoch
        ):
            return control
        raise ConflictError(
            code="cloud_f_already_ready",
            message="Cloud/F control was already activated with different coordinates",
        )
    if control.change_epoch != expected_epoch:
        raise ConflictError(
            code="cloud_f_epoch_mismatch",
            message="Cloud/F change epoch no longer matches",
        )
    if control.lease_operation_id is not None or control.active_operations:
        raise ConflictError(
            code="cloud_f_operations_active",
            message="Cloud/F control cannot activate while operations are active",
        )
    cursor = await db.execute(
        "UPDATE cloud_f_control SET change_epoch=?,readiness_state='ready',"
        "readiness_subtype=?,activated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND readiness_state='bootstrap_required' AND change_epoch=?",
        (activated_epoch, subtype, workspace_id, expected_epoch),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        refreshed = await read_cloud_f_control(db, workspace_id=workspace_id)
        if (
            refreshed.readiness_state == "ready"
            and refreshed.readiness_subtype == subtype
            and refreshed.change_epoch == activated_epoch
        ):
            return refreshed
        raise ConflictError(
            code="cloud_f_activation_conflict",
            message="Cloud/F readiness changed during activation",
        )
    await db.commit()
    return await read_cloud_f_control(db, workspace_id=workspace_id)


async def acquire_cloud_f_change(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    operation_id: str,
    operation_kind: str,
    expected_epoch: int,
    lease_expires_at: datetime,
) -> CloudFControlSnapshot:
    """Acquire the common exclusive lease and register one active operation."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    op_kind = _validate_token("operation_kind", operation_kind, _OPERATION_KIND_RE)
    lease_expiry = _future_timestamp("lease", lease_expires_at)
    actor = ctx.user_id or ctx.username
    control = await read_cloud_f_control(db, workspace_id=workspace_id)
    journal = await _cloud_f_change_operation_row(
        db,
        workspace_id=workspace_id,
        operation_id=op_id,
    )
    if journal is not None and journal["state"] == "completed":
        raise ConflictError(
            code="cloud_f_operation_id_reused",
            message="Cloud/F operation ID already belongs to a completed change",
        )
    if control.readiness_state != "ready":
        raise ConflictError(
            code="cloud_f_not_ready",
            message="Cloud/F lease has not been activated",
        )
    if control.change_epoch != expected_epoch:
        raise ConflictError(
            code="cloud_f_epoch_mismatch",
            message="Cloud/F change epoch no longer matches",
        )
    now = _iso(_utc_now())
    same_operation = control.lease_operation_id == op_id
    lease_live = bool(
        control.lease_operation_id
        and control.lease_expires_at
        and str(control.lease_expires_at) > now
    )
    if lease_live and not same_operation:
        raise ConflictError(
            code="cloud_f_lease_busy",
            message="Another protected Cloud/F operation owns the lease",
        )
    if control.active_operations and not (
        len(control.active_operations) == 1
        and control.active_operations[0]["operation_id"] == op_id
    ):
        raise ConflictError(
            code="cloud_f_operations_active",
            message="Another protected Cloud/F operation is active",
        )
    active = control.active_operations[0] if control.active_operations else None
    if same_operation and active is None:
        raise ServiceUnavailableError(
            code="cloud_f_operation_registry_mismatch",
            message="Cloud/F lease has no matching active-operation record",
        )
    if active is not None:
        active_mismatches: list[str] = []
        if active["operation_id"] != op_id:
            active_mismatches.append("operation_id")
        if active["operation_kind"] != op_kind:
            active_mismatches.append("operation_kind")
        if active["actor"] != actor:
            active_mismatches.append("actor")
        if int(active["base_epoch"]) != expected_epoch:
            active_mismatches.append("base_epoch")
        if active_mismatches:
            raise ConflictError(
                code="cloud_f_operation_conflict",
                message="Cloud/F operation coordinates do not match",
                context={"mismatches": active_mismatches},
            )
        if journal is None:
            raise ServiceUnavailableError(
                code="cloud_f_operation_registry_mismatch",
                message="Cloud/F active operation has no durable journal",
            )
        journal_mismatches: list[str] = []
        if journal["state"] != "active":
            journal_mismatches.append("state")
        if journal["operation_kind"] != op_kind:
            journal_mismatches.append("operation_kind")
        if journal["actor"] != actor:
            journal_mismatches.append("actor")
        if int(journal["base_epoch"]) != expected_epoch:
            journal_mismatches.append("base_epoch")
        if journal_mismatches:
            raise ConflictError(
                code="cloud_f_operation_conflict",
                message="Cloud/F durable operation coordinates do not match",
                context={"mismatches": journal_mismatches},
            )
    elif journal is not None:
        raise ServiceUnavailableError(
            code="cloud_f_operation_registry_mismatch",
            message="Cloud/F durable operation lost its active registry entry",
        )
    if same_operation and control.lease_owner != actor:
        raise ConflictError(
            code="cloud_f_operation_conflict",
            message="Cloud/F lease owner does not match the operation actor",
            context={"mismatches": ["actor"]},
        )

    generation = (
        control.lease_generation
        if same_operation
        else control.lease_generation + 1
    )
    if same_operation:
        lease_predicate = "lease_operation_id=?"
        lease_params: tuple[Any, ...] = (op_id,)
    else:
        lease_predicate = (
            "(lease_operation_id IS NULL OR lease_expires_at IS NULL "
            "OR lease_expires_at<=?)"
        )
        lease_params = (now,)
    cursor = await db.execute(
        "UPDATE cloud_f_control SET lease_generation=?,lease_operation_id=?,"
        "lease_owner=?,lease_expires_at=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND change_epoch=? AND lease_generation=? AND "
        + lease_predicate,
        (
            generation,
            op_id,
            actor,
            _iso(lease_expiry),
            workspace_id,
            expected_epoch,
            control.lease_generation,
            *lease_params,
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="cloud_f_lease_busy",
            message="Cloud/F lease changed while it was being acquired",
        )
    if active is None:
        try:
            await db.execute(
                "INSERT INTO cloud_f_change_operations "
                "(workspace_id,operation_id,operation_kind,actor,base_epoch,"
                "lease_generation,state) VALUES (?,?,?,?,?,?,'active')",
                (workspace_id, op_id, op_kind, actor, expected_epoch, generation),
            )
            await db.execute(
                "INSERT INTO cloud_f_active_operations "
                "(workspace_id,operation_id,operation_kind,actor,base_epoch,lease_generation) "
                "VALUES (?,?,?,?,?,?)",
                (workspace_id, op_id, op_kind, actor, expected_epoch, generation),
            )
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ConflictError(
                code="cloud_f_operation_conflict",
                message="Cloud/F operation ID is already active",
            ) from exc
    else:
        cursor = await db.execute(
            "UPDATE cloud_f_active_operations SET lease_generation=? "
            "WHERE workspace_id=? AND operation_id=? AND operation_kind=? "
            "AND actor=? AND base_epoch=?",
            (generation, workspace_id, op_id, op_kind, actor, expected_epoch),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="cloud_f_operation_conflict",
                message="Cloud/F operation changed while its lease was renewed",
            )
        cursor = await db.execute(
            "UPDATE cloud_f_change_operations SET lease_generation=?,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND operation_id=? AND state='active' "
            "AND operation_kind=? AND actor=? AND base_epoch=?",
            (generation, workspace_id, op_id, op_kind, actor, expected_epoch),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="cloud_f_operation_conflict",
                message="Cloud/F durable operation changed during renewal",
            )
    await db.commit()
    return await read_cloud_f_control(db, workspace_id=workspace_id)


async def complete_cloud_f_change(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    operation_id: str,
    expected_epoch: int,
    advance_epoch: bool = True,
) -> CloudFControlSnapshot:
    """Release the exact lease; successful protected changes advance epoch."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    actor = ctx.user_id or ctx.username
    journal = await _cloud_f_change_operation_row(
        db,
        workspace_id=workspace_id,
        operation_id=op_id,
    )
    if journal is None:
        raise NotFoundError(
            code="cloud_f_operation_not_found",
            message="Cloud/F operation was not found",
        )
    journal_mismatches: list[str] = []
    if journal["actor"] != actor:
        journal_mismatches.append("actor")
    if int(journal["base_epoch"]) != expected_epoch:
        journal_mismatches.append("base_epoch")
    if journal["state"] == "completed" and bool(journal["advance_epoch"]) != bool(
        advance_epoch
    ):
        journal_mismatches.append("advance_epoch")
    if journal_mismatches:
        raise ConflictError(
            code="cloud_f_operation_conflict",
            message="Cloud/F operation coordinates do not match",
            context={"mismatches": journal_mismatches},
        )
    if journal["state"] == "completed":
        return _cloud_f_result_snapshot(journal)
    control = await read_cloud_f_control(db, workspace_id=workspace_id)
    if control.lease_operation_id != op_id:
        raise ConflictError(
            code="cloud_f_lease_not_owned",
            message="Operation does not own the Cloud/F lease",
        )
    if (
        control.lease_expires_at is None
        or str(control.lease_expires_at) <= _iso(_utc_now())
    ):
        raise ConflictError(
            code="cloud_f_lease_expired",
            message="Cloud/F lease expired and must be renewed before completion",
        )
    if control.change_epoch != expected_epoch:
        raise ConflictError(
            code="cloud_f_epoch_mismatch",
            message="Cloud/F change epoch no longer matches",
        )
    if control.lease_owner != actor:
        raise ConflictError(
            code="cloud_f_lease_not_owned",
            message="Caller does not own the Cloud/F lease",
        )
    if not (
        len(control.active_operations) == 1
        and control.active_operations[0]["operation_id"] == op_id
        and control.active_operations[0]["actor"] == actor
        and int(control.active_operations[0]["base_epoch"]) == expected_epoch
    ):
        raise ConflictError(
            code="cloud_f_operation_registry_mismatch",
            message="Cloud/F active-operation record does not match the lease",
        )
    result_snapshot = CloudFControlSnapshot(
        workspace_id=workspace_id,
        change_epoch=expected_epoch + (1 if advance_epoch else 0),
        readiness_state=control.readiness_state,
        readiness_subtype=control.readiness_subtype,
        lease_generation=control.lease_generation,
        lease_operation_id=None,
        lease_owner=None,
        lease_expires_at=None,
        active_operations=(),
        active_operations_digest=_EMPTY_ACTIVE_OPERATIONS_DIGEST,
    )
    completion_now = _iso(_utc_now())
    if str(control.lease_expires_at) <= completion_now:
        raise ConflictError(
            code="cloud_f_lease_expired",
            message="Cloud/F lease expired and must be renewed before completion",
        )
    cursor = await db.execute(
        "UPDATE cloud_f_control SET change_epoch=?,lease_operation_id=NULL,"
        "lease_owner=NULL,lease_expires_at=NULL,"
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND lease_operation_id=? AND lease_owner=? "
        "AND change_epoch=? AND lease_generation=? AND lease_expires_at>?",
        (
            expected_epoch + (1 if advance_epoch else 0),
            workspace_id,
            op_id,
            actor,
            expected_epoch,
            control.lease_generation,
            completion_now,
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="cloud_f_lease_changed",
            message="Cloud/F lease changed before completion",
        )
    cursor = await db.execute(
        "DELETE FROM cloud_f_active_operations WHERE workspace_id=? "
        "AND operation_id=? AND actor=? AND base_epoch=? AND lease_generation=?",
        (
            workspace_id,
            op_id,
            actor,
            expected_epoch,
            control.lease_generation,
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="cloud_f_operation_registry_mismatch",
            message="Cloud/F active-operation record changed before completion",
        )
    cursor = await db.execute(
        "UPDATE cloud_f_change_operations SET state='completed',advance_epoch=?,"
        "result_epoch=?,result_json=?,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND operation_id=? AND state='active' "
        "AND actor=? AND base_epoch=? AND lease_generation=?",
        (
            1 if advance_epoch else 0,
            result_snapshot.change_epoch,
            json.dumps(
                snapshot_dict(result_snapshot),
                sort_keys=True,
                separators=(",", ":"),
            ),
            workspace_id,
            op_id,
            actor,
            expected_epoch,
            control.lease_generation,
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="cloud_f_operation_conflict",
            message="Cloud/F durable operation changed before completion",
        )
    await db.commit()
    return result_snapshot


async def read_cloud_f_change_operation(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
) -> dict[str, Any]:
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    row = await _cloud_f_change_operation_row(
        db,
        workspace_id=workspace_id,
        operation_id=op_id,
    )
    if row is None:
        raise NotFoundError(
            code="cloud_f_operation_not_found",
            message="Cloud/F operation was not found",
        )
    result = _row_dict(row) or {}
    if result.get("result_json"):
        result["result"] = json.loads(result.pop("result_json"))
    else:
        result.pop("result_json", None)
        result["result"] = None
    return result


async def update_project_selector_watermark(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    operation_id: str,
    expected_epoch: int,
    expected_selector_watermark: str,
    selector_watermark: str,
    projects_root: Path | None = None,
) -> ProjectLifecycleSnapshot:
    """Bind a verified selector snapshot while its protected lease is held."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    slug = _validate_slug(project_slug)
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    next_watermark = _validate_digest("selector_watermark", selector_watermark)
    current_watermark = (expected_selector_watermark or "").strip()
    if len(current_watermark) > 256:
        raise ValidationError(
            code="invalid_selector_watermark",
            message="Expected selector watermark is too long",
        )
    if current_watermark == next_watermark:
        raise ValidationError(
            code="selector_watermark_unchanged",
            message="Selector watermark already matches the requested value",
        )
    actor = ctx.user_id or ctx.username

    async with async_project_mutation_guard(projects_root=projects_root):
        control = await read_cloud_f_control(db, workspace_id=workspace_id)
        if (
            control.readiness_state != "ready"
            or control.change_epoch != expected_epoch
            or control.lease_operation_id != op_id
            or control.lease_owner != actor
            or len(control.active_operations) != 1
            or control.active_operations[0]["operation_id"] != op_id
            or control.active_operations[0]["actor"] != actor
            or int(control.active_operations[0]["base_epoch"]) != expected_epoch
        ):
            raise ConflictError(
                code="cloud_f_lease_not_owned",
                message="Selector update requires the caller's exact Cloud/F lease",
            )
        if (
            control.lease_expires_at is None
            or str(control.lease_expires_at) <= _iso(_utc_now())
        ):
            raise ConflictError(
                code="cloud_f_lease_expired",
                message="Cloud/F lease expired and must be renewed before mutation",
            )
        state = await ensure_project_lifecycle(
            db,
            workspace_id=workspace_id,
            project_slug=slug,
            projects_root=projects_root,
        )
        if state.lifecycle == "archived" or state.transition_operation_id is not None:
            raise ConflictError(
                code="project_not_writable",
                message="Project is archived or a lifecycle transition is active",
            )
        if state.selector_watermark == next_watermark:
            cursor = await db.execute(
                "SELECT 1 FROM project_write_events WHERE workspace_id=? "
                "AND project_slug=? AND operation_id=? "
                "AND writer_kind='selector_snapshot' AND resource_ref=? LIMIT 1",
                (workspace_id, slug, op_id, next_watermark),
            )
            if await cursor.fetchone() is None:
                raise ConflictError(
                    code="selector_compare_and_set_failed",
                    message="Selector watermark belongs to a different operation",
                )
            return state
        if state.selector_watermark != current_watermark:
            raise ConflictError(
                code="selector_compare_and_set_failed",
                message="Selector watermark changed before update",
            )
        await record_project_write(
            db,
            workspace_id=workspace_id,
            project_slug=slug,
            writer_kind="selector_snapshot",
            actor=actor,
            resource_ref=next_watermark,
            operation_id=op_id,
            projects_root=projects_root,
        )
        mutation_now = _iso(_utc_now())
        if str(control.lease_expires_at) <= mutation_now:
            await db.rollback()
            raise ConflictError(
                code="cloud_f_lease_expired",
                message="Cloud/F lease expired and must be renewed before mutation",
            )
        cursor = await db.execute(
            "UPDATE project_lifecycle_state SET selector_watermark=?,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND project_slug=? AND selector_watermark=? "
            "AND lifecycle!='archived' AND transition_operation_id IS NULL "
            "AND EXISTS (SELECT 1 FROM cloud_f_control control "
            "WHERE control.workspace_id=? AND control.change_epoch=? "
            "AND control.lease_generation=? AND control.lease_operation_id=? "
            "AND control.lease_owner=? AND control.lease_expires_at>?)",
            (
                next_watermark,
                workspace_id,
                slug,
                current_watermark,
                workspace_id,
                expected_epoch,
                control.lease_generation,
                op_id,
                actor,
                mutation_now,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="selector_compare_and_set_failed",
                message="Selector watermark changed during update",
            )
        await db.commit()
        return await read_project_lifecycle(
            db,
            workspace_id=workspace_id,
            project_slug=slug,
        )


async def create_archive_approval(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    expected_project_id: str,
    expected_project_digest: str,
    plan_f_digest: str,
    master_digest: str,
    evidence_digest: str,
    expected_writer_watermark: int,
    expected_selector_watermark: str,
    expected_cloud_f_epoch: int,
    expected_active_operations_digest: str,
    expires_at: datetime,
    approval_id: str | None = None,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Persist explicit approval bound to the complete archive CAS state."""
    require_role_ctx(ctx, "admin", "super_admin")
    authority = await resolve_approval_authority(ctx, db)
    if authority is None:
        raise AuthorizationError(
            code="approval_authority_required",
            message="Archive approval requires human or persisted delegated approval",
        )
    approval_expiry = _future_timestamp("approval", expires_at)
    workspace_id = require_workspace_ctx(ctx)
    slug = _validate_slug(project_slug)
    identifier = (
        _validate_token("approval_id", approval_id, _OPERATION_ID_RE)
        if approval_id is not None
        else "apr_" + uuid.uuid4().hex
    )
    expected_project_digest = _validate_digest(
        "expected_project_digest", expected_project_digest
    )
    plan_f_digest = _validate_digest("plan_f_digest", plan_f_digest)
    master_digest = _validate_digest("master_digest", master_digest)
    evidence_digest = _validate_digest("evidence_digest", evidence_digest)
    expected_active_operations_digest = _validate_digest(
        "expected_active_operations_digest", expected_active_operations_digest
    )
    actor = ctx.user_id or ctx.username
    expected_expiry = _iso(approval_expiry)

    async with async_project_mutation_guard(projects_root=projects_root):
        if approval_id is not None:
            existing = await _approval_row(db, identifier)
            if existing is not None:
                expected = {
                    "workspace_id": workspace_id,
                    "project_id": expected_project_id,
                    "project_slug": slug,
                    "expected_project_digest": expected_project_digest,
                    "plan_f_digest": plan_f_digest,
                    "master_digest": master_digest,
                    "evidence_digest": evidence_digest,
                    "expected_writer_watermark": expected_writer_watermark,
                    "expected_selector_watermark": expected_selector_watermark,
                    "expected_cloud_f_epoch": expected_cloud_f_epoch,
                    "expected_active_operations_digest": (
                        expected_active_operations_digest
                    ),
                    "approved_by": actor,
                    "authority_kind": authority.kind,
                    "authority_grant_id": authority.grant_id,
                    "expires_at": expected_expiry,
                }
                replay_mismatches = [
                    field for field, value in expected.items() if existing[field] != value
                ]
                if replay_mismatches:
                    raise ConflictError(
                        code="archive_approval_idempotency_conflict",
                        message="Archive approval ID was used with other coordinates",
                        context={"mismatches": replay_mismatches},
                    )
                return _archive_approval_result(existing)

        state = await ensure_project_lifecycle(
            db,
            workspace_id=workspace_id,
            project_slug=slug,
            projects_root=projects_root,
        )
        control = await ensure_cloud_f_control(db, workspace_id=workspace_id)
        mismatches: list[str] = []
        if state.project_id != expected_project_id:
            mismatches.append("project_id")
        if state.lifecycle == "archived":
            mismatches.append("lifecycle")
        if state.transition_operation_id is not None:
            mismatches.append("transition")
        live_digest = project_digest(project_directory(slug, projects_root=projects_root))
        if live_digest != expected_project_digest:
            mismatches.append("project_digest")
        if state.writer_watermark != expected_writer_watermark:
            mismatches.append("writer_watermark")
        if state.selector_watermark != expected_selector_watermark:
            mismatches.append("selector_watermark")
        if control.readiness_state != "ready":
            mismatches.append("lease_readiness")
        if control.change_epoch != expected_cloud_f_epoch:
            mismatches.append("cloud_f_change_epoch")
        if control.lease_operation_id is not None:
            mismatches.append("lease_owner")
        if control.active_operations_digest != expected_active_operations_digest:
            mismatches.append("active_operations")
        if control.active_operations:
            mismatches.append("active_operations_not_empty")
        if mismatches:
            raise ConflictError(
                code="archive_approval_state_mismatch",
                message="Archive approval coordinates are stale",
                context={"mismatches": sorted(set(mismatches))},
            )
        await db.execute(
            "INSERT INTO project_archive_approvals "
            "(approval_id,workspace_id,project_id,project_slug,expected_lifecycle,"
            "expected_project_digest,plan_f_digest,master_digest,evidence_digest,"
            "expected_writer_watermark,expected_selector_watermark,"
            "expected_cloud_f_epoch,expected_active_operations_digest,approved_by,"
            "authority_kind,authority_grant_id,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                workspace_id,
                state.project_id,
                slug,
                state.lifecycle,
                expected_project_digest,
                plan_f_digest,
                master_digest,
                evidence_digest,
                expected_writer_watermark,
                expected_selector_watermark,
                expected_cloud_f_epoch,
                expected_active_operations_digest,
                actor,
                authority.kind,
                authority.grant_id,
                expected_expiry,
            ),
        )
        await db.commit()
        persisted = await _approval_row(db, identifier)
        if persisted is None:  # pragma: no cover - defensive storage invariant
            raise ServiceUnavailableError(
                code="archive_approval_persistence_failed",
                message="Archive approval could not be read after creation",
            )
        return _archive_approval_result(persisted)


async def _approval_row(
    db: aiosqlite.Connection,
    approval_id: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM project_archive_approvals WHERE approval_id=?",
        (approval_id,),
    )
    return await cursor.fetchone()


def _archive_approval_result(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "approval_id": row["approval_id"],
        "project_id": row["project_id"],
        "project_slug": row["project_slug"],
        "expected_project_digest": row["expected_project_digest"],
        "expected_writer_watermark": row["expected_writer_watermark"],
        "expected_selector_watermark": row["expected_selector_watermark"],
        "expected_cloud_f_epoch": row["expected_cloud_f_epoch"],
        "expected_active_operations_digest": row[
            "expected_active_operations_digest"
        ],
        "expires_at": row["expires_at"],
        "authority_kind": row["authority_kind"],
    }


def _archive_request_payload(
    *,
    project_slug: str,
    project_id: str,
    approval_id: str,
    expected_project_digest: str,
    plan_f_digest: str,
    master_digest: str,
    evidence_digest: str,
    expected_writer_watermark: int,
    expected_selector_watermark: str,
    expected_cloud_f_epoch: int,
    expected_active_operations_digest: str,
) -> dict[str, Any]:
    return {
        "project_slug": project_slug,
        "project_id": project_id,
        "approval_id": approval_id,
        "expected_project_digest": expected_project_digest,
        "plan_f_digest": plan_f_digest,
        "master_digest": master_digest,
        "evidence_digest": evidence_digest,
        "expected_writer_watermark": expected_writer_watermark,
        "expected_selector_watermark": expected_selector_watermark,
        "expected_cloud_f_epoch": expected_cloud_f_epoch,
        "expected_active_operations_digest": expected_active_operations_digest,
    }


async def _existing_lifecycle_operation(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
    idempotency_key: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM project_lifecycle_operations "
        "WHERE workspace_id=? AND (operation_id=? OR idempotency_key=?)",
        (workspace_id, operation_id, idempotency_key),
    )
    return await cursor.fetchone()


async def archive_project(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    project_id: str,
    approval_id: str,
    expected_project_digest: str,
    plan_f_digest: str,
    master_digest: str,
    evidence_digest: str,
    expected_writer_watermark: int,
    expected_selector_watermark: str,
    expected_cloud_f_epoch: int,
    expected_active_operations_digest: str,
    operation_id: str,
    idempotency_key: str,
    lease_expires_at: datetime,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Atomically archive a logical project without moving or deleting data."""
    require_role_ctx(ctx, "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    actor = ctx.user_id or ctx.username
    slug = _validate_slug(project_slug)
    operation_id = _validate_token(
        "operation_id", operation_id, _OPERATION_ID_RE
    )
    idempotency_key = _validate_idempotency_key(idempotency_key)
    digests = {
        "expected_project_digest": _validate_digest(
            "expected_project_digest", expected_project_digest
        ),
        "plan_f_digest": _validate_digest("plan_f_digest", plan_f_digest),
        "master_digest": _validate_digest("master_digest", master_digest),
        "evidence_digest": _validate_digest("evidence_digest", evidence_digest),
        "expected_active_operations_digest": _validate_digest(
            "expected_active_operations_digest", expected_active_operations_digest
        ),
    }
    payload = _archive_request_payload(
        project_slug=slug,
        project_id=project_id,
        approval_id=approval_id,
        expected_project_digest=digests["expected_project_digest"],
        plan_f_digest=digests["plan_f_digest"],
        master_digest=digests["master_digest"],
        evidence_digest=digests["evidence_digest"],
        expected_writer_watermark=expected_writer_watermark,
        expected_selector_watermark=expected_selector_watermark,
        expected_cloud_f_epoch=expected_cloud_f_epoch,
        expected_active_operations_digest=digests[
            "expected_active_operations_digest"
        ],
    )
    request_digest = _canonical_digest(payload)
    project_dir = project_directory(slug, projects_root=projects_root)

    async with async_project_mutation_guard(projects_root=projects_root):
        existing = await _existing_lifecycle_operation(
            db,
            workspace_id=workspace_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            if existing["actor"] != actor:
                raise ConflictError(
                    code="archive_actor_mismatch",
                    message="Lifecycle retry must use the original operation actor",
                )
            if existing["request_digest"] != request_digest:
                raise ConflictError(
                    code="idempotency_conflict",
                    message="Lifecycle idempotency key was used with another request",
                )
            if existing["operation_id"] != operation_id:
                raise ConflictError(
                    code="idempotency_operation_mismatch",
                    message="Lifecycle retry must reuse the original operation ID",
                )
            if existing["idempotency_key"] != idempotency_key:
                raise ConflictError(
                    code="idempotency_key_mismatch",
                    message="Lifecycle retry must reuse the original idempotency key",
                )
            if existing["state"] == "completed":
                return json.loads(existing["result_json"] or "{}")
            if existing["state"] not in {"prepared", "filesystem_applied"}:
                raise ConflictError(
                    code="archive_recovery_state_mismatch",
                    message="Lifecycle operation is not recoverable from its current state",
                )
            try:
                checkpoint = json.loads(existing["result_json"] or "{}")
                checkpoint_digest = str(checkpoint["filesystem_project_digest"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ServiceUnavailableError(
                    code="archive_operation_corrupt",
                    message="Archive filesystem checkpoint is invalid",
                ) from exc
            if not _HEX_64_RE.fullmatch(checkpoint_digest):
                raise ServiceUnavailableError(
                    code="archive_operation_corrupt",
                    message="Archive filesystem checkpoint failed integrity validation",
                )
            expected_archived_digest = checkpoint_digest

        lease_expiry = _future_timestamp("lease", lease_expires_at)

        state = await read_project_lifecycle(
            db,
            workspace_id=workspace_id,
            project_slug=slug,
        )
        approval = await _approval_row(db, approval_id)
        control = await read_cloud_f_control(db, workspace_id=workspace_id)

        if existing is None:
            mismatches: list[str] = []
            if approval is None:
                raise NotFoundError(
                    code="archive_approval_not_found",
                    message="Archive approval was not found",
                )
            if state.project_id != project_id or approval["project_id"] != project_id:
                mismatches.append("project_id")
            if approval["project_slug"] != slug or approval["workspace_id"] != workspace_id:
                mismatches.append("project_scope")
            if state.lifecycle != approval["expected_lifecycle"] or state.lifecycle == "archived":
                mismatches.append("lifecycle")
            if state.transition_operation_id is not None:
                mismatches.append("transition")
            live_metadata, live_project_yaml = _read_project_yaml(project_dir)
            live_project_digest = hashlib.sha256(live_project_yaml).hexdigest()
            archived_metadata = dict(live_metadata)
            archived_metadata["lifecycle"] = "archived"
            expected_archived_digest = hashlib.sha256(
                _render_project_yaml(archived_metadata)
            ).hexdigest()
            for key in (
                "expected_project_digest",
                "plan_f_digest",
                "master_digest",
                "evidence_digest",
                "expected_active_operations_digest",
            ):
                if str(approval[key]) != str(digests[key]):
                    mismatches.append(key)
            if live_project_digest != digests["expected_project_digest"]:
                mismatches.append("project_digest")
            if state.writer_watermark != expected_writer_watermark or int(
                approval["expected_writer_watermark"]
            ) != expected_writer_watermark:
                mismatches.append("writer_watermark")
            if state.selector_watermark != expected_selector_watermark or str(
                approval["expected_selector_watermark"]
            ) != expected_selector_watermark:
                mismatches.append("selector_watermark")
            if control.readiness_state != "ready":
                mismatches.append("lease_readiness")
            if control.change_epoch != expected_cloud_f_epoch or int(
                approval["expected_cloud_f_epoch"]
            ) != expected_cloud_f_epoch:
                mismatches.append("cloud_f_change_epoch")
            if control.lease_operation_id is not None:
                mismatches.append("lease_owner")
            if control.active_operations_digest != digests[
                "expected_active_operations_digest"
            ]:
                mismatches.append("active_operations")
            if control.active_operations:
                mismatches.append("active_operations_not_empty")
            if approval["consumed_by_operation_id"] is not None:
                mismatches.append("approval_consumed")
            if str(approval["expires_at"]) <= _iso(_utc_now()):
                mismatches.append("approval_expired")
            if mismatches:
                raise ConflictError(
                    code="archive_compare_and_set_failed",
                    message="Archive coordinates changed after approval",
                    context={"mismatches": sorted(set(mismatches))},
                )

            generation = control.lease_generation + 1
            cursor = await db.execute(
                "UPDATE cloud_f_control SET lease_generation=?,lease_operation_id=?,"
                "lease_owner=?,lease_expires_at=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE workspace_id=? AND change_epoch=? AND readiness_state='ready' "
                "AND lease_operation_id IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM cloud_f_active_operations active "
                "WHERE active.workspace_id=cloud_f_control.workspace_id)",
                (
                    generation,
                    operation_id,
                    actor,
                    _iso(lease_expiry),
                    workspace_id,
                    expected_cloud_f_epoch,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ConflictError(
                    code="cloud_f_lease_busy",
                    message="Cloud/F lease changed while archive was starting",
                )
            try:
                await db.execute(
                    "INSERT INTO cloud_f_active_operations "
                    "(workspace_id,operation_id,operation_kind,actor,base_epoch,lease_generation) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        workspace_id,
                        operation_id,
                        "project_archive",
                        actor,
                        expected_cloud_f_epoch,
                        generation,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise ConflictError(
                    code="cloud_f_operation_conflict",
                    message="Archive operation ID is already active",
                ) from exc
            cursor = await db.execute(
                "UPDATE project_lifecycle_state SET transition_operation_id=?,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE workspace_id=? AND project_slug=? AND project_id=? "
                "AND lifecycle=? AND writer_watermark=? AND selector_watermark=? "
                "AND transition_operation_id IS NULL",
                (
                    operation_id,
                    workspace_id,
                    slug,
                    project_id,
                    approval["expected_lifecycle"],
                    expected_writer_watermark,
                    expected_selector_watermark,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ConflictError(
                    code="archive_compare_and_set_failed",
                    message="Project lifecycle changed while archive was starting",
                    context={"mismatches": ["project_state"]},
                )
            cursor = await db.execute(
                "UPDATE project_archive_approvals SET consumed_by_operation_id=?,"
                "consumed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE approval_id=? AND consumed_by_operation_id IS NULL",
                (operation_id, approval_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ConflictError(
                    code="archive_compare_and_set_failed",
                    message="Archive approval changed while archive was starting",
                    context={"mismatches": ["approval_consumed"]},
                )
            await db.execute(
                "INSERT INTO project_lifecycle_operations "
                "(workspace_id,operation_id,idempotency_key,operation_kind,actor,project_id,"
                "project_slug,approval_id,request_digest,state,result_json) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'prepared',?)",
                (
                    workspace_id,
                    operation_id,
                    idempotency_key,
                    "archive",
                    actor,
                    project_id,
                    slug,
                    approval_id,
                    request_digest,
                    json.dumps(
                        {"filesystem_project_digest": expected_archived_digest},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            await db.commit()
        else:
            # Safe recovery of the exact prepared operation.  Renew only its own
            # persisted lease; no different operation may coexist with the fence.
            if state.transition_operation_id != operation_id:
                raise ConflictError(
                    code="archive_recovery_state_mismatch",
                    message="Lifecycle operation cannot be safely resumed",
                )
            if control.lease_operation_id not in {None, operation_id}:
                raise ConflictError(
                    code="cloud_f_lease_busy",
                    message="Another protected Cloud/F operation owns the lease",
                )
            foreign_operations = [
                item
                for item in control.active_operations
                if item["operation_id"] != operation_id
            ]
            matching_operations = [
                item
                for item in control.active_operations
                if item["operation_id"] == operation_id
            ]
            if foreign_operations or len(matching_operations) != 1:
                raise ConflictError(
                    code="archive_recovery_state_mismatch",
                    message="Archive active-operation registry cannot be safely resumed",
                )
            active = matching_operations[0]
            if (
                active["operation_kind"] != "project_archive"
                or active["actor"] != actor
                or int(active["base_epoch"]) != expected_cloud_f_epoch
                or int(active["lease_generation"]) != control.lease_generation
                or (
                    control.lease_operation_id == operation_id
                    and control.lease_owner != actor
                )
            ):
                raise ConflictError(
                    code="archive_recovery_state_mismatch",
                    message="Archive active-operation coordinates changed",
                )
            generation = control.lease_generation
            if control.lease_operation_id is None:
                generation += 1
            cursor = await db.execute(
                "UPDATE cloud_f_control SET lease_generation=?,lease_operation_id=?,lease_owner=?,"
                "lease_expires_at=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE workspace_id=? AND change_epoch=? AND lease_generation=? "
                "AND (lease_operation_id IS NULL OR "
                "(lease_operation_id=? AND lease_owner=?))",
                (
                    generation,
                    operation_id,
                    ctx.user_id or ctx.username,
                    _iso(lease_expiry),
                    workspace_id,
                    expected_cloud_f_epoch,
                    control.lease_generation,
                    operation_id,
                    actor,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ConflictError(
                    code="cloud_f_lease_busy",
                    message="Cloud/F lease changed during archive recovery",
                )
            cursor = await db.execute(
                "UPDATE cloud_f_active_operations SET lease_generation=? "
                "WHERE workspace_id=? AND operation_id=? AND operation_kind='project_archive' "
                "AND actor=? AND base_epoch=?",
                (
                    generation,
                    workspace_id,
                    operation_id,
                    actor,
                    expected_cloud_f_epoch,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ConflictError(
                    code="archive_recovery_state_mismatch",
                    message="Archive active-operation registry changed during recovery",
                )
            await db.commit()

        metadata, current_project_yaml = _read_project_yaml(project_dir)
        current_digest = hashlib.sha256(current_project_yaml).hexdigest()
        if str(metadata.get("lifecycle") or "active") != "archived":
            if current_digest != digests["expected_project_digest"]:
                raise ConflictError(
                    code="archive_recovery_content_mismatch",
                    message="Project metadata changed after archive preparation",
                )
            metadata["lifecycle"] = "archived"
            archived_digest = _atomic_write_project_yaml(project_dir, metadata)
        else:
            archived_digest = current_digest
        if archived_digest != expected_archived_digest:
            raise ConflictError(
                code="archive_recovery_content_mismatch",
                message="Archived project metadata does not match its checkpoint",
            )
        cursor = await db.execute(
            "UPDATE project_lifecycle_operations SET state='filesystem_applied',"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND operation_id=? AND state='prepared'",
            (workspace_id, operation_id),
        )
        if existing is None or existing["state"] == "prepared":
            if cursor.rowcount != 1:
                await db.rollback()
                raise ConflictError(
                    code="archive_recovery_state_mismatch",
                    message="Archive filesystem checkpoint changed unexpectedly",
                )
        await db.commit()

        completion_now = _iso(_utc_now())
        final_control = await read_cloud_f_control(db, workspace_id=workspace_id)
        if (
            final_control.change_epoch != expected_cloud_f_epoch
            or final_control.lease_generation != generation
            or final_control.lease_operation_id != operation_id
            or final_control.lease_owner != actor
            or len(final_control.active_operations) != 1
            or final_control.active_operations[0]["operation_id"] != operation_id
            or final_control.active_operations[0]["operation_kind"]
            != "project_archive"
            or final_control.active_operations[0]["actor"] != actor
            or int(final_control.active_operations[0]["base_epoch"])
            != expected_cloud_f_epoch
            or int(final_control.active_operations[0]["lease_generation"])
            != generation
        ):
            raise ConflictError(
                code="archive_finalize_state_mismatch",
                message="Archive lease or operation coordinates changed before completion",
            )
        if (
            final_control.lease_expires_at is None
            or str(final_control.lease_expires_at) <= completion_now
        ):
            raise ConflictError(
                code="cloud_f_lease_expired",
                message="Cloud/F lease expired and must be renewed before completion",
            )

        archived_at = completion_now
        result = {
            "operation_id": operation_id,
            "project_id": project_id,
            "project_slug": slug,
            "lifecycle": "archived",
            "project_digest": archived_digest,
            "previous_cloud_f_epoch": expected_cloud_f_epoch,
            "cloud_f_change_epoch": expected_cloud_f_epoch + 1,
            "writer_watermark": expected_writer_watermark,
            "selector_watermark": expected_selector_watermark,
            "approval_id": approval_id,
            "archived_at": archived_at,
        }
        cursor = await db.execute(
            "UPDATE project_lifecycle_state SET lifecycle='archived',project_digest=?,"
            "transition_operation_id=NULL,archived_at=?,archived_by=?,"
            "archive_approval_id=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND project_slug=? AND project_id=? "
            "AND transition_operation_id=?",
            (
                archived_digest,
                archived_at,
                actor,
                approval_id,
                workspace_id,
                slug,
                project_id,
                operation_id,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="archive_finalize_state_mismatch",
                message="Project transition fence changed before archive completion",
            )
        cursor = await db.execute(
            "UPDATE project_lifecycle_operations SET state='completed',result_json=?,"
            "error_code=NULL,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND operation_id=? AND state='filesystem_applied'",
            (
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                workspace_id,
                operation_id,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="archive_finalize_state_mismatch",
                message="Archive operation step changed before completion",
            )
        cursor = await db.execute(
            "DELETE FROM cloud_f_active_operations WHERE workspace_id=? "
            "AND operation_id=? AND actor=?",
            (workspace_id, operation_id, actor),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="archive_finalize_state_mismatch",
                message="Archive active-operation registry changed before completion",
            )
        cursor = await db.execute(
            "UPDATE cloud_f_control SET change_epoch=?,lease_operation_id=NULL,"
            "lease_owner=NULL,lease_expires_at=NULL,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND lease_operation_id=? AND change_epoch=? "
            "AND lease_generation=? AND lease_owner=? AND lease_expires_at>?",
            (
                expected_cloud_f_epoch + 1,
                workspace_id,
                operation_id,
                expected_cloud_f_epoch,
                generation,
                actor,
                completion_now,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="cloud_f_epoch_mismatch",
                message="Cloud/F lease changed before archive completion",
            )
        await db.commit()
        return result


async def read_lifecycle_operation(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
) -> dict[str, Any]:
    cursor = await db.execute(
        "SELECT workspace_id,operation_id,idempotency_key,operation_kind,actor,project_id,"
        "project_slug,approval_id,request_digest,state,result_json,error_code,"
        "created_at,updated_at,completed_at FROM project_lifecycle_operations "
        "WHERE workspace_id=? AND operation_id=?",
        (workspace_id, operation_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise NotFoundError(
            code="lifecycle_operation_not_found",
            message="Lifecycle operation was not found",
        )
    result = _row_dict(row) or {}
    if result.get("result_json"):
        result["result"] = json.loads(result.pop("result_json"))
    else:
        result.pop("result_json", None)
        result["result"] = None
    return result


def snapshot_dict(snapshot: ProjectLifecycleSnapshot | CloudFControlSnapshot) -> dict[str, Any]:
    """JSON-safe shared adapter helper."""
    payload = asdict(snapshot)
    if isinstance(snapshot, CloudFControlSnapshot):
        payload["active_operations"] = list(snapshot.active_operations)
    return payload


__all__ = [
    "CloudFControlSnapshot",
    "ProjectLifecycleSnapshot",
    "_EMPTY_ACTIVE_OPERATIONS_DIGEST",
    "acquire_cloud_f_change",
    "async_project_mutation_guard",
    "activate_cloud_f_control",
    "archive_project",
    "assert_project_writable",
    "complete_cloud_f_change",
    "create_archive_approval",
    "ensure_cloud_f_control",
    "ensure_project_lifecycle",
    "guarded_project_file_write",
    "project_digest",
    "project_directory",
    "project_mutation_guard",
    "read_cloud_f_control",
    "read_cloud_f_change_operation",
    "read_lifecycle_operation",
    "read_project_lifecycle",
    "record_project_write",
    "snapshot_dict",
    "update_project_selector_watermark",
]
