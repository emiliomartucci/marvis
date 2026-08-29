"""Governed decision lifecycle and additive historical pointers.

The service is transport independent.  HTTP and MCP call the same operations;
SQLite persists a resumable saga while the shared project lock protects files.
Every mutation owns the common Cloud/F lease, advances its epoch exactly once,
and fences each affected project until the operation is completed or resumed.

Decision bodies are immutable through accept/supersede transitions.  Only YAML
frontmatter is rewritten, and the normalized body digest is verified before and
after each filesystem step.  Historical handoff/learning pointers are additive
database records: their source artifact is never moved, deleted, or rewritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import aiosqlite
import yaml

from core.api.services import project_lifecycle
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

_DECISION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_FRONTMATTER_RE = re.compile(
    rb"\A---\r?\n(?P<yaml>.*?)\r?\n---\r?\n",
    re.DOTALL,
)


@dataclass(frozen=True)
class DocumentSnapshot:
    path: Path
    relative_path: str
    raw: bytes
    frontmatter: dict[str, Any] | None
    body: bytes
    content_digest: str
    body_digest: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _canonical_digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _validate_token(name: str, value: str, pattern: re.Pattern[str]) -> str:
    normalized = (value or "").strip()
    if not pattern.fullmatch(normalized):
        raise ValidationError(
            code=f"invalid_{name}",
            message=f"Invalid {name.replace('_', ' ')}",
        )
    return normalized


def _validate_digest(name: str, value: str) -> str:
    normalized = (value or "").strip().lower()
    if not _HEX_64_RE.fullmatch(normalized):
        raise ValidationError(
            code="invalid_digest",
            message=f"{name} must be a lowercase SHA-256 digest",
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


def _validate_relative_path(relative_path: str, *, require_markdown: bool) -> str:
    raw = (relative_path or "").strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if (
        not raw
        or pure.is_absolute()
        or raw in {".", ".."}
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValidationError(
            code="invalid_artifact_path",
            message="Artifact path must be a project-relative path",
        )
    if require_markdown and pure.suffix.lower() != ".md":
        raise ValidationError(
            code="invalid_decision_path",
            message="Governed decisions must use a .md path",
        )
    return pure.as_posix()


def _artifact_path(
    project_slug: str,
    relative_path: str,
    *,
    projects_root: Path | None,
    require_markdown: bool,
) -> tuple[Path, str, Path]:
    normalized = _validate_relative_path(
        relative_path,
        require_markdown=require_markdown,
    )
    project_dir = project_lifecycle.project_directory(
        project_slug,
        projects_root=projects_root,
    )
    candidate = project_dir.joinpath(*PurePosixPath(normalized).parts)
    current = project_dir
    for part in PurePosixPath(normalized).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValidationError(
                code="artifact_symlink_denied",
                message="Artifact path traverses a symbolic link",
            )
    if candidate.exists() and candidate.is_symlink():
        raise ValidationError(
            code="artifact_symlink_denied",
            message="Artifact path is a symbolic link",
        )
    return candidate, normalized, project_dir


def _normalized_body_digest(body: bytes) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ServiceError(
            code="artifact_not_utf8",
            message="Governed artifact must be UTF-8 text",
        ) from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_document(path: Path, relative_path: str) -> DocumentSnapshot:
    if not path.is_file() or path.is_symlink():
        raise NotFoundError(
            code="artifact_not_found",
            message="Governed artifact was not found",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ServiceUnavailableError(
            code="artifact_unavailable",
            message="Governed artifact cannot be read",
        ) from exc
    match = _FRONTMATTER_RE.match(raw)
    frontmatter: dict[str, Any] | None = None
    body = raw
    if match is not None:
        try:
            parsed = yaml.safe_load(match.group("yaml").decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ServiceError(
                code="artifact_frontmatter_invalid",
                message="Governed artifact frontmatter is invalid",
            ) from exc
        if not isinstance(parsed, dict):
            raise ServiceError(
                code="artifact_frontmatter_invalid",
                message="Governed artifact frontmatter must be a mapping",
            )
        frontmatter = parsed
        body = raw[match.end():]
    return DocumentSnapshot(
        path=path,
        relative_path=relative_path,
        raw=raw,
        frontmatter=frontmatter,
        body=body,
        content_digest=hashlib.sha256(raw).hexdigest(),
        body_digest=_normalized_body_digest(body),
    )


def _render_document(frontmatter: Mapping[str, Any], body: bytes) -> bytes:
    dumped = yaml.safe_dump(
        dict(frontmatter),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n")
    return b"---\n" + dumped.encode("utf-8") + b"\n---\n" + body


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    if path.exists():
        mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


async def _operation_row(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
    idempotency_key: str,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT * FROM decision_lifecycle_operations "
        "WHERE workspace_id=? AND (operation_id=? OR idempotency_key=?)",
        (workspace_id, operation_id, idempotency_key),
    )
    return await cursor.fetchone()


def _completed_result(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None or row["state"] != "completed":
        return None
    return json.loads(row["result_json"] or "{}")


def _verify_existing_operation(
    row: aiosqlite.Row,
    *,
    actor: str,
    operation_id: str,
    idempotency_key: str,
    operation_kind: str,
    primary_project_slug: str,
    request_digest: str,
    expected_cloud_f_epoch: int,
) -> None:
    mismatches: list[str] = []
    if str(row["actor"]) != actor:
        mismatches.append("actor")
    if str(row["operation_id"]) != operation_id:
        mismatches.append("operation_id")
    if str(row["idempotency_key"]) != idempotency_key:
        mismatches.append("idempotency_key")
    if str(row["operation_kind"]) != operation_kind:
        mismatches.append("operation_kind")
    if str(row["primary_project_slug"]) != primary_project_slug:
        mismatches.append("project_slug")
    if str(row["request_digest"]) != request_digest:
        mismatches.append("request_digest")
    if int(row["cloud_f_epoch"]) != expected_cloud_f_epoch:
        mismatches.append("cloud_f_change_epoch")
    if mismatches:
        raise ConflictError(
            code="decision_idempotency_conflict",
            message="Decision idempotency coordinates do not match",
            context={"mismatches": mismatches},
        )


def _verified_operation_request(row: aiosqlite.Row) -> dict[str, Any]:
    try:
        request = json.loads(str(row["request_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ServiceUnavailableError(
            code="decision_operation_corrupt",
            message="Decision operation request journal is invalid",
        ) from exc
    if not isinstance(request, dict) or _canonical_digest(request) != str(
        row["request_digest"]
    ):
        raise ServiceUnavailableError(
            code="decision_operation_corrupt",
            message="Decision operation request journal failed integrity validation",
        )
    return request


def _completed_operation_replay(
    row: aiosqlite.Row | None,
    *,
    actor: str,
    operation_id: str,
    idempotency_key: str,
    operation_kind: str,
    primary_project_slug: str,
    expected_cloud_f_epoch: int,
    expected_request: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a completed result without consulting mutable live artifacts."""
    if row is None:
        return None
    request = _verified_operation_request(row)
    _verify_existing_operation(
        row,
        actor=actor,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        operation_kind=operation_kind,
        primary_project_slug=primary_project_slug,
        request_digest=str(row["request_digest"]),
        expected_cloud_f_epoch=expected_cloud_f_epoch,
    )
    mismatches = [
        key for key, value in expected_request.items() if request.get(key) != value
    ]
    if mismatches:
        raise ConflictError(
            code="decision_idempotency_conflict",
            message="Decision idempotency coordinates do not match",
            context={"mismatches": mismatches},
        )
    return _completed_result(row)


def _future_lease_expiry(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(
            code="lease_timezone_required",
            message="Cloud/F lease expiry must include a timezone",
        )
    normalized = value.astimezone(timezone.utc)
    if normalized <= _utc_now():
        raise ValidationError(
            code="lease_expired",
            message="Cloud/F lease expiry must be in the future",
        )
    return normalized


async def _start_or_resume_operation(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    row: aiosqlite.Row | None,
    operation_id: str,
    idempotency_key: str,
    operation_kind: str,
    primary_project_slug: str,
    affected_projects: Mapping[str, str],
    request_payload: Mapping[str, Any],
    request_digest: str,
    expected_cloud_f_epoch: int,
    lease_expires_at: datetime,
    projects_root: Path | None,
) -> aiosqlite.Row:
    workspace_id = require_workspace_ctx(ctx)
    actor = ctx.user_id or ctx.username
    lease_expiry = _future_lease_expiry(lease_expires_at)
    control = await project_lifecycle.read_cloud_f_control(
        db,
        workspace_id=workspace_id,
    )
    if control.readiness_state != "ready":
        raise ConflictError(
            code="cloud_f_not_ready",
            message="Cloud/F lease has not been activated",
        )
    if control.change_epoch != expected_cloud_f_epoch:
        raise ConflictError(
            code="cloud_f_epoch_mismatch",
            message="Cloud/F change epoch no longer matches",
        )

    if row is None:
        if control.lease_operation_id is not None or control.active_operations:
            raise ConflictError(
                code="cloud_f_lease_busy",
                message="Another protected Cloud/F operation is active",
            )
        for project_slug in affected_projects:
            state = await project_lifecycle.ensure_project_lifecycle(
                db,
                workspace_id=workspace_id,
                project_slug=project_slug,
                projects_root=projects_root,
            )
            if state.lifecycle == "archived" or state.transition_operation_id is not None:
                raise ConflictError(
                    code="project_not_writable",
                    message="Project is archived or a lifecycle transition is active",
                )

        generation = control.lease_generation + 1
        cursor = await db.execute(
            "UPDATE cloud_f_control SET lease_generation=?,lease_operation_id=?,"
            "lease_owner=?,lease_expires_at=?,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND readiness_state='ready' AND change_epoch=? "
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
                message="Cloud/F lease changed while the operation was starting",
            )
        await db.execute(
            "INSERT INTO cloud_f_active_operations "
            "(workspace_id,operation_id,operation_kind,actor,base_epoch,lease_generation) "
            "VALUES (?,?,?,?,?,?)",
            (
                workspace_id,
                operation_id,
                f"decision_{operation_kind}",
                actor,
                expected_cloud_f_epoch,
                generation,
            ),
        )
        for project_slug, resource_ref in affected_projects.items():
            await project_lifecycle.record_project_write(
                db,
                workspace_id=workspace_id,
                project_slug=project_slug,
                writer_kind=f"decision_{operation_kind}",
                actor=actor,
                resource_ref=resource_ref,
                operation_id=operation_id,
                projects_root=projects_root,
            )
            cursor = await db.execute(
                "UPDATE project_lifecycle_state SET transition_operation_id=?,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE workspace_id=? AND project_slug=? "
                "AND lifecycle!='archived' AND transition_operation_id IS NULL",
                (operation_id, workspace_id, project_slug),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ConflictError(
                    code="project_transition_conflict",
                    message="Project lifecycle changed while the operation was starting",
                )
        await db.execute(
            "INSERT INTO decision_lifecycle_operations "
            "(workspace_id,operation_id,idempotency_key,operation_kind,"
            "primary_project_slug,actor,cloud_f_epoch,request_json,request_digest,state) "
            "VALUES (?,?,?,?,?,?,?,?,?,'prepared')",
            (
                workspace_id,
                operation_id,
                idempotency_key,
                operation_kind,
                primary_project_slug,
                actor,
                expected_cloud_f_epoch,
                _canonical_json(request_payload),
                request_digest,
            ),
        )
        await db.commit()
    else:
        _verify_existing_operation(
            row,
            actor=actor,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            operation_kind=operation_kind,
            primary_project_slug=primary_project_slug,
            request_digest=request_digest,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
        )
        foreign_operations = [
            item
            for item in control.active_operations
            if item["operation_id"] != operation_id
        ]
        if foreign_operations or control.lease_operation_id not in {None, operation_id}:
            raise ConflictError(
                code="cloud_f_lease_busy",
                message="Another protected Cloud/F operation is active",
            )
        matching_operations = [
            item
            for item in control.active_operations
            if item["operation_id"] == operation_id
        ]
        if len(matching_operations) != 1:
            raise ConflictError(
                code="decision_recovery_state_mismatch",
                message="Decision active-operation registry cannot be safely resumed",
            )
        active = matching_operations[0]
        if (
            active["operation_kind"] != f"decision_{operation_kind}"
            or active["actor"] != actor
            or int(active["base_epoch"]) != expected_cloud_f_epoch
            or int(active["lease_generation"]) != control.lease_generation
            or (
                control.lease_operation_id == operation_id
                and control.lease_owner != actor
            )
        ):
            raise ConflictError(
                code="decision_recovery_state_mismatch",
                message="Decision active-operation coordinates changed",
            )
        for project_slug in affected_projects:
            state = await project_lifecycle.read_project_lifecycle(
                db,
                workspace_id=workspace_id,
                project_slug=project_slug,
            )
            if state.transition_operation_id != operation_id:
                raise ConflictError(
                    code="decision_recovery_state_mismatch",
                    message="Decision operation cannot be safely resumed",
                )
        generation = control.lease_generation
        if control.lease_operation_id is None:
            generation += 1
        cursor = await db.execute(
            "UPDATE cloud_f_control SET lease_generation=?,lease_operation_id=?,"
            "lease_owner=?,lease_expires_at=?,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND change_epoch=? AND lease_generation=? "
            "AND (lease_operation_id IS NULL OR "
            "(lease_operation_id=? AND lease_owner=?))",
            (
                generation,
                operation_id,
                actor,
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
                message="Cloud/F lease changed during decision recovery",
            )
        cursor = await db.execute(
            "UPDATE cloud_f_active_operations SET lease_generation=? "
            "WHERE workspace_id=? AND operation_id=? "
            "AND operation_kind=? AND actor=? AND base_epoch=?",
            (
                generation,
                workspace_id,
                operation_id,
                f"decision_{operation_kind}",
                actor,
                expected_cloud_f_epoch,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="decision_recovery_state_mismatch",
                message="Decision active-operation registry changed",
            )
        await db.commit()

    refreshed = await _operation_row(
        db,
        workspace_id=workspace_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )
    if refreshed is None:  # pragma: no cover - defensive corruption guard
        raise ServiceUnavailableError(
            code="decision_operation_unavailable",
            message="Decision operation journal is unavailable",
        )
    return refreshed


async def _mark_filesystem_applied(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
) -> None:
    cursor = await db.execute(
        "UPDATE decision_lifecycle_operations SET state='filesystem_applied',"
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND operation_id=? AND state='prepared'",
        (workspace_id, operation_id),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="decision_recovery_state_mismatch",
            message="Decision filesystem checkpoint changed unexpectedly",
        )
    await db.commit()


async def _complete_operation(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
    actor: str,
    affected_projects: Sequence[str],
    expected_cloud_f_epoch: int,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    cursor = await db.execute(
        "SELECT operation_kind,actor FROM decision_lifecycle_operations "
        "WHERE workspace_id=? AND operation_id=? AND state='filesystem_applied'",
        (workspace_id, operation_id),
    )
    operation = await cursor.fetchone()
    control = await project_lifecycle.read_cloud_f_control(
        db,
        workspace_id=workspace_id,
    )
    if (
        operation is None
        or operation["actor"] != actor
        or control.change_epoch != expected_cloud_f_epoch
        or control.lease_operation_id != operation_id
        or control.lease_owner != actor
        or len(control.active_operations) != 1
        or control.active_operations[0]["operation_id"] != operation_id
        or control.active_operations[0]["operation_kind"]
        != f"decision_{operation['operation_kind']}"
        or control.active_operations[0]["actor"] != actor
        or int(control.active_operations[0]["base_epoch"])
        != expected_cloud_f_epoch
        or int(control.active_operations[0]["lease_generation"])
        != control.lease_generation
    ):
        raise ConflictError(
            code="decision_finalize_state_mismatch",
            message="Decision lease or operation coordinates changed before completion",
        )
    completion_now = _iso(_utc_now())
    if (
        control.lease_expires_at is None
        or str(control.lease_expires_at) <= completion_now
    ):
        raise ConflictError(
            code="cloud_f_lease_expired",
            message="Cloud/F lease expired and must be renewed before completion",
        )
    for project_slug in affected_projects:
        cursor = await db.execute(
            "UPDATE project_lifecycle_state SET transition_operation_id=NULL,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND project_slug=? AND transition_operation_id=?",
            (workspace_id, project_slug, operation_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                code="decision_finalize_state_mismatch",
                message="Project transition fence changed before completion",
            )
    cursor = await db.execute(
        "UPDATE decision_lifecycle_operations SET state='completed',result_json=?,"
        "error_code=NULL,completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND operation_id=? AND actor=? "
        "AND state='filesystem_applied'",
        (_canonical_json(result), workspace_id, operation_id, actor),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="decision_finalize_state_mismatch",
            message="Decision operation step changed before completion",
        )
    cursor = await db.execute(
        "DELETE FROM cloud_f_active_operations WHERE workspace_id=? "
        "AND operation_id=? AND actor=? AND base_epoch=? AND lease_generation=?",
        (
            workspace_id,
            operation_id,
            actor,
            expected_cloud_f_epoch,
            control.lease_generation,
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="decision_finalize_state_mismatch",
            message="Decision active-operation registry changed before completion",
        )
    cursor = await db.execute(
        "UPDATE cloud_f_control SET change_epoch=?,lease_operation_id=NULL,"
        "lease_owner=NULL,lease_expires_at=NULL,"
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE workspace_id=? AND lease_operation_id=? AND lease_owner=? "
        "AND change_epoch=? AND lease_generation=? AND lease_expires_at>?",
        (
            expected_cloud_f_epoch + 1,
            workspace_id,
            operation_id,
            actor,
            expected_cloud_f_epoch,
            control.lease_generation,
            completion_now,
        ),
    )
    if cursor.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            code="cloud_f_epoch_mismatch",
            message="Cloud/F epoch changed before decision completion",
        )
    await db.commit()
    return dict(result)


async def _require_approval_authority(
    ctx: CallerContext,
    db: aiosqlite.Connection,
) -> Any:
    authority = await resolve_approval_authority(ctx, db)
    if authority is None:
        raise AuthorizationError(
            code="approval_authority_required",
            message="Decision lifecycle change requires human or delegated approval",
        )
    return authority


async def create_decision(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    relative_path: str,
    title: str,
    body: str,
    operation_id: str,
    idempotency_key: str,
    expected_cloud_f_epoch: int,
    lease_expires_at: datetime,
    decision_id: str | None = None,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Create one draft decision and advance the protected-change epoch."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    actor = ctx.user_id or ctx.username
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    identifier = decision_id or (
        "dec_" + hashlib.sha256(f"{workspace_id}:{op_id}".encode()).hexdigest()[:32]
    )
    identifier = _validate_token("decision_id", identifier, _DECISION_ID_RE)
    if not title.strip():
        raise ValidationError(code="decision_title_required", message="Decision title is required")
    target, rel_path, project_dir = _artifact_path(
        project_slug,
        relative_path,
        projects_root=projects_root,
        require_markdown=True,
    )
    body_bytes = body.encode("utf-8")
    payload = {
        "decision_id": identifier,
        "project_slug": project_slug,
        "relative_path": rel_path,
        "title": title.strip(),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "body_digest": _normalized_body_digest(body_bytes),
        "expected_cloud_f_epoch": expected_cloud_f_epoch,
    }
    request_digest = _canonical_digest(payload)

    async with project_lifecycle.async_project_mutation_guard(
        projects_root=project_dir.parent
    ):
        target, rel_path, project_dir = _artifact_path(
            project_slug,
            relative_path,
            projects_root=project_dir.parent,
            require_markdown=True,
        )
        row = await _operation_row(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            idempotency_key=idempotency_key,
        )
        if row is not None:
            _verify_existing_operation(
                row,
                actor=actor,
                operation_id=op_id,
                idempotency_key=idempotency_key,
                operation_kind="create",
                primary_project_slug=project_slug,
                request_digest=request_digest,
                expected_cloud_f_epoch=expected_cloud_f_epoch,
            )
            completed = _completed_result(row)
            if completed is not None:
                return completed
        else:
            cursor = await db.execute(
                "SELECT 1 FROM governed_decisions WHERE workspace_id=? "
                "AND (decision_id=? OR (project_slug=? AND relative_path=?))",
                (workspace_id, identifier, project_slug, rel_path),
            )
            if await cursor.fetchone() is not None or target.exists():
                raise ConflictError(
                    code="decision_exists",
                    message="Decision ID or path already exists",
                )

        row = await _start_or_resume_operation(
            ctx,
            db,
            row=row,
            operation_id=op_id,
            idempotency_key=idempotency_key,
            operation_kind="create",
            primary_project_slug=project_slug,
            affected_projects={project_slug: rel_path},
            request_payload=payload,
            request_digest=request_digest,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            lease_expires_at=lease_expires_at,
            projects_root=project_dir.parent,
        )
        frontmatter = {
            "decision_id": identifier,
            "title": title.strip(),
            "project": project_slug,
            "lifecycle": "draft",
            "created_by": actor,
            "created_at": str(row["created_at"]),
        }
        expected_document = _render_document(frontmatter, body_bytes)
        if row["state"] == "prepared":
            if target.exists():
                current = _parse_document(target, rel_path)
                if (
                    current.raw != expected_document
                    or current.body_digest != payload["body_digest"]
                ):
                    raise ConflictError(
                        code="decision_recovery_conflict",
                        message="Prepared decision path contains different content",
                    )
            else:
                _atomic_write(target, expected_document)
            await _mark_filesystem_applied(
                db,
                workspace_id=workspace_id,
                operation_id=op_id,
            )
        snapshot = _parse_document(target, rel_path)
        try:
            await db.execute(
                "INSERT OR IGNORE INTO governed_decisions "
                "(workspace_id,decision_id,project_slug,relative_path,content_digest,"
                "body_digest,lifecycle,created_by) VALUES (?,?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    identifier,
                    project_slug,
                    rel_path,
                    snapshot.content_digest,
                    snapshot.body_digest,
                    "draft",
                    actor,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(
                code="decision_exists",
                message="Decision ID or path already exists",
            ) from exc
        cursor = await db.execute(
            "SELECT project_slug,relative_path,content_digest,body_digest,lifecycle "
            "FROM governed_decisions WHERE workspace_id=? AND decision_id=?",
            (workspace_id, identifier),
        )
        stored = await cursor.fetchone()
        if stored is None or (
            stored["project_slug"] != project_slug
            or stored["relative_path"] != rel_path
            or stored["content_digest"] != snapshot.content_digest
            or stored["body_digest"] != snapshot.body_digest
            or stored["lifecycle"] != "draft"
        ):
            raise ConflictError(
                code="decision_identity_conflict",
                message="Decision record conflicts with the prepared file",
            )
        result = {
            "operation_id": op_id,
            "decision_id": identifier,
            "project_slug": project_slug,
            "relative_path": rel_path,
            "lifecycle": "draft",
            "content_digest": snapshot.content_digest,
            "body_digest": snapshot.body_digest,
            "previous_cloud_f_epoch": expected_cloud_f_epoch,
            "cloud_f_change_epoch": expected_cloud_f_epoch + 1,
        }
        return await _complete_operation(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            actor=actor,
            affected_projects=[project_slug],
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            result=result,
        )


async def accept_decision(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    decision_id: str,
    expected_content_digest: str,
    operation_id: str,
    idempotency_key: str,
    expected_cloud_f_epoch: int,
    lease_expires_at: datetime,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Accept a draft decision while preserving its body byte-for-byte."""
    require_role_ctx(ctx, "admin", "super_admin")
    authority = await _require_approval_authority(ctx, db)
    workspace_id = require_workspace_ctx(ctx)
    actor = ctx.user_id or ctx.username
    identifier = _validate_token("decision_id", decision_id, _DECISION_ID_RE)
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    expected_digest = _validate_digest("expected_content_digest", expected_content_digest)
    replay_request = {
        "decision_id": identifier,
        "project_slug": project_slug,
        "expected_content_digest": expected_digest,
        "expected_cloud_f_epoch": expected_cloud_f_epoch,
    }
    row = await _operation_row(
        db,
        workspace_id=workspace_id,
        operation_id=op_id,
        idempotency_key=idempotency_key,
    )
    completed = _completed_operation_replay(
        row,
        actor=actor,
        operation_id=op_id,
        idempotency_key=idempotency_key,
        operation_kind="accept",
        primary_project_slug=project_slug,
        expected_cloud_f_epoch=expected_cloud_f_epoch,
        expected_request=replay_request,
    )
    if completed is not None:
        return completed

    project_dir = project_lifecycle.project_directory(
        project_slug,
        projects_root=projects_root,
    )

    async with project_lifecycle.async_project_mutation_guard(
        projects_root=project_dir.parent
    ):
        row = await _operation_row(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            idempotency_key=idempotency_key,
        )
        completed = _completed_operation_replay(
            row,
            actor=actor,
            operation_id=op_id,
            idempotency_key=idempotency_key,
            operation_kind="accept",
            primary_project_slug=project_slug,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            expected_request=replay_request,
        )
        if completed is not None:
            return completed

        cursor = await db.execute(
            "SELECT * FROM governed_decisions WHERE workspace_id=? AND decision_id=? "
            "AND project_slug=?",
            (workspace_id, identifier, project_slug),
        )
        decision = await cursor.fetchone()
        if decision is None:
            raise NotFoundError(code="decision_not_found", message="Decision was not found")
        target, rel_path, _project_dir = _artifact_path(
            project_slug,
            str(decision["relative_path"]),
            projects_root=project_dir.parent,
            require_markdown=True,
        )
        payload = {
            **replay_request,
            "relative_path": rel_path,
        }
        request_digest = _canonical_digest(payload)
        if row is not None:
            _verify_existing_operation(
                row,
                actor=actor,
                operation_id=op_id,
                idempotency_key=idempotency_key,
                operation_kind="accept",
                primary_project_slug=project_slug,
                request_digest=request_digest,
                expected_cloud_f_epoch=expected_cloud_f_epoch,
            )
        elif decision["lifecycle"] != "draft" or decision["content_digest"] != expected_digest:
            raise ConflictError(
                code="decision_compare_and_set_failed",
                message="Decision lifecycle or content digest changed",
            )

        before = _parse_document(target, rel_path)
        if row is None and before.content_digest != expected_digest:
            raise ConflictError(
                code="decision_compare_and_set_failed",
                message="Decision file digest changed",
            )
        row = await _start_or_resume_operation(
            ctx,
            db,
            row=row,
            operation_id=op_id,
            idempotency_key=idempotency_key,
            operation_kind="accept",
            primary_project_slug=project_slug,
            affected_projects={project_slug: rel_path},
            request_payload=payload,
            request_digest=request_digest,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            lease_expires_at=lease_expires_at,
            projects_root=project_dir.parent,
        )
        if row["state"] == "prepared":
            current = _parse_document(target, rel_path)
            frontmatter = dict(current.frontmatter or {})
            already_applied = (
                frontmatter.get("decision_id") == identifier
                and frontmatter.get("lifecycle") == "accepted"
                and current.body_digest == str(decision["body_digest"])
            )
            if not already_applied:
                if current.content_digest != expected_digest:
                    raise ConflictError(
                        code="decision_recovery_conflict",
                        message="Decision changed after acceptance was prepared",
                    )
                frontmatter.update(
                    {
                        "decision_id": identifier,
                        "project": project_slug,
                        "lifecycle": "accepted",
                        "accepted_by": actor,
                        "accepted_at": str(row["created_at"]),
                        "acceptance_authority": authority.kind,
                    }
                )
                _atomic_write(target, _render_document(frontmatter, current.body))
            after = _parse_document(target, rel_path)
            if after.body_digest != str(decision["body_digest"]):
                raise ConflictError(
                    code="decision_body_changed",
                    message="Decision body changed during acceptance",
                )
            await _mark_filesystem_applied(
                db,
                workspace_id=workspace_id,
                operation_id=op_id,
            )
        after = _parse_document(target, rel_path)
        cursor = await db.execute(
            "UPDATE governed_decisions SET lifecycle='accepted',content_digest=?,"
            "accepted_by=?,accepted_at=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE workspace_id=? AND decision_id=? AND lifecycle='draft'",
            (
                after.content_digest,
                actor,
                str(row["created_at"]),
                workspace_id,
                identifier,
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                code="decision_finalize_state_mismatch",
                message="Decision record changed before acceptance completed",
            )
        result = {
            "operation_id": op_id,
            "decision_id": identifier,
            "project_slug": project_slug,
            "relative_path": rel_path,
            "lifecycle": "accepted",
            "content_digest": after.content_digest,
            "body_digest": after.body_digest,
            "previous_cloud_f_epoch": expected_cloud_f_epoch,
            "cloud_f_change_epoch": expected_cloud_f_epoch + 1,
        }
        return await _complete_operation(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            actor=actor,
            affected_projects=[project_slug],
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            result=result,
        )


async def supersede_decision(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    source_project_slug: str,
    source_decision_id: str,
    source_relative_path: str,
    expected_source_content_digest: str,
    expected_source_body_digest: str,
    target_project_slug: str,
    target_decision_id: str,
    expected_target_content_digest: str,
    operation_id: str,
    idempotency_key: str,
    expected_cloud_f_epoch: int,
    lease_expires_at: datetime,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Supersede one accepted/legacy decision with an accepted target decision."""
    require_role_ctx(ctx, "admin", "super_admin")
    await _require_approval_authority(ctx, db)
    workspace_id = require_workspace_ctx(ctx)
    actor = ctx.user_id or ctx.username
    source_id = _validate_token("decision_id", source_decision_id, _DECISION_ID_RE)
    target_id = _validate_token("decision_id", target_decision_id, _DECISION_ID_RE)
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    source_content_digest = _validate_digest(
        "expected_source_content_digest", expected_source_content_digest
    )
    source_body_digest = _validate_digest(
        "expected_source_body_digest", expected_source_body_digest
    )
    target_content_digest = _validate_digest(
        "expected_target_content_digest", expected_target_content_digest
    )
    source_rel = _validate_relative_path(source_relative_path, require_markdown=True)
    replay_request = {
        "source_project_slug": source_project_slug,
        "source_decision_id": source_id,
        "source_relative_path": source_rel,
        "expected_source_content_digest": source_content_digest,
        "expected_source_body_digest": source_body_digest,
        "target_project_slug": target_project_slug,
        "target_decision_id": target_id,
        "expected_target_content_digest": target_content_digest,
        "expected_cloud_f_epoch": expected_cloud_f_epoch,
    }
    row = await _operation_row(
        db,
        workspace_id=workspace_id,
        operation_id=op_id,
        idempotency_key=idempotency_key,
    )
    completed = _completed_operation_replay(
        row,
        actor=actor,
        operation_id=op_id,
        idempotency_key=idempotency_key,
        operation_kind="supersede",
        primary_project_slug=source_project_slug,
        expected_cloud_f_epoch=expected_cloud_f_epoch,
        expected_request=replay_request,
    )
    if completed is not None:
        return completed

    source_dir = project_lifecycle.project_directory(
        source_project_slug,
        projects_root=projects_root,
    )

    async with project_lifecycle.async_project_mutation_guard(
        projects_root=source_dir.parent
    ):
        row = await _operation_row(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            idempotency_key=idempotency_key,
        )
        completed = _completed_operation_replay(
            row,
            actor=actor,
            operation_id=op_id,
            idempotency_key=idempotency_key,
            operation_kind="supersede",
            primary_project_slug=source_project_slug,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            expected_request=replay_request,
        )
        if completed is not None:
            return completed

        source_path, source_rel, _source_dir = _artifact_path(
            source_project_slug,
            source_rel,
            projects_root=source_dir.parent,
            require_markdown=True,
        )
        cursor = await db.execute(
            "SELECT * FROM governed_decisions WHERE workspace_id=? AND decision_id=? "
            "AND project_slug=?",
            (workspace_id, target_id, target_project_slug),
        )
        target = await cursor.fetchone()
        if target is None or target["lifecycle"] != "accepted":
            raise ConflictError(
                code="replacement_decision_not_accepted",
                message="Replacement decision must exist and be accepted",
            )
        target_path, target_rel, _target_dir = _artifact_path(
            target_project_slug,
            str(target["relative_path"]),
            projects_root=source_dir.parent,
            require_markdown=True,
        )
        target_snapshot = _parse_document(target_path, target_rel)
        if (
            str(target["content_digest"]) != target_content_digest
            or target_snapshot.content_digest != target_content_digest
        ):
            raise ConflictError(
                code="replacement_decision_changed",
                message="Replacement decision digest changed",
            )
        payload = {
            **replay_request,
            "target_relative_path": target_rel,
        }
        request_digest = _canonical_digest(payload)
        if row is not None:
            _verify_existing_operation(
                row,
                actor=actor,
                operation_id=op_id,
                idempotency_key=idempotency_key,
                operation_kind="supersede",
                primary_project_slug=source_project_slug,
                request_digest=request_digest,
                expected_cloud_f_epoch=expected_cloud_f_epoch,
            )
        cursor = await db.execute(
            "SELECT * FROM governed_decisions WHERE workspace_id=? AND decision_id=?",
            (workspace_id, source_id),
        )
        source_record = await cursor.fetchone()
        before = _parse_document(source_path, source_rel)
        if source_record is not None and (
            source_record["project_slug"] != source_project_slug
            or source_record["relative_path"] != source_rel
            or source_record["lifecycle"] != "accepted"
        ):
            raise ConflictError(
                code="source_decision_not_accepted",
                message="Source decision is not an accepted decision",
            )
        if row is None:
            if before.content_digest != source_content_digest or before.body_digest != source_body_digest:
                raise ConflictError(
                    code="decision_compare_and_set_failed",
                    message="Source decision digest changed",
                )
        row = await _start_or_resume_operation(
            ctx,
            db,
            row=row,
            operation_id=op_id,
            idempotency_key=idempotency_key,
            operation_kind="supersede",
            primary_project_slug=source_project_slug,
            affected_projects={source_project_slug: source_rel},
            request_payload=payload,
            request_digest=request_digest,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            lease_expires_at=lease_expires_at,
            projects_root=source_dir.parent,
        )
        pointer_path = f"{target_project_slug}/{target_rel}"
        if row["state"] == "prepared":
            current = _parse_document(source_path, source_rel)
            frontmatter = dict(current.frontmatter or {})
            already_applied = (
                frontmatter.get("lifecycle") == "superseded"
                and frontmatter.get("superseded_by_decision_id") == target_id
                and frontmatter.get("superseded_by_path") == pointer_path
                and current.body_digest == source_body_digest
            )
            if not already_applied:
                if current.content_digest != source_content_digest or current.body_digest != source_body_digest:
                    raise ConflictError(
                        code="decision_recovery_conflict",
                        message="Source decision changed after supersession was prepared",
                    )
                frontmatter.update(
                    {
                        "decision_id": source_id,
                        "project": source_project_slug,
                        "lifecycle": "superseded",
                        "superseded_by": pointer_path,
                        "superseded_by_decision_id": target_id,
                        "superseded_by_project": target_project_slug,
                        "superseded_by_path": pointer_path,
                        "superseded_at": str(row["created_at"]),
                    }
                )
                _atomic_write(source_path, _render_document(frontmatter, current.body))
            after = _parse_document(source_path, source_rel)
            if after.body_digest != source_body_digest:
                raise ConflictError(
                    code="decision_body_changed",
                    message="Source decision body changed during supersession",
                )
            await _mark_filesystem_applied(
                db,
                workspace_id=workspace_id,
                operation_id=op_id,
            )
        after = _parse_document(source_path, source_rel)
        try:
            await db.execute(
                "INSERT INTO governed_decisions "
                "(workspace_id,decision_id,project_slug,relative_path,content_digest,"
                "body_digest,lifecycle,created_by,accepted_by,accepted_at,"
                "superseded_by_decision_id,superseded_by_project_slug,"
                "superseded_by_path,superseded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id,decision_id) DO UPDATE SET "
                "content_digest=excluded.content_digest,body_digest=excluded.body_digest,"
                "lifecycle='superseded',superseded_by_decision_id=excluded.superseded_by_decision_id,"
                "superseded_by_project_slug=excluded.superseded_by_project_slug,"
                "superseded_by_path=excluded.superseded_by_path,"
                "superseded_at=excluded.superseded_at,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (
                    workspace_id,
                    source_id,
                    source_project_slug,
                    source_rel,
                    after.content_digest,
                    after.body_digest,
                    "superseded",
                    actor,
                    (source_record["accepted_by"] if source_record else "historical-import"),
                    (source_record["accepted_at"] if source_record else str(row["created_at"])),
                    target_id,
                    target_project_slug,
                    pointer_path,
                    str(row["created_at"]),
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(
                code="decision_identity_conflict",
                message="Source decision ID/path conflicts with another decision",
            ) from exc
        await db.execute(
            "INSERT OR IGNORE INTO historical_artifact_pointers "
            "(workspace_id,operation_id,source_project_slug,source_kind,"
            "source_relative_path,source_body_digest,relation,target_project_slug,"
            "target_decision_id,target_relative_path,created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                workspace_id,
                op_id,
                source_project_slug,
                "decision",
                source_rel,
                source_body_digest,
                "forward",
                target_project_slug,
                target_id,
                target_rel,
                actor,
            ),
        )
        result = {
            "operation_id": op_id,
            "source_decision_id": source_id,
            "source_project_slug": source_project_slug,
            "source_relative_path": source_rel,
            "lifecycle": "superseded",
            "content_digest": after.content_digest,
            "body_digest": after.body_digest,
            "target_decision_id": target_id,
            "target_project_slug": target_project_slug,
            "target_relative_path": target_rel,
            "previous_cloud_f_epoch": expected_cloud_f_epoch,
            "cloud_f_change_epoch": expected_cloud_f_epoch + 1,
        }
        return await _complete_operation(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            actor=actor,
            affected_projects=[source_project_slug],
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            result=result,
        )


async def _learning_body_digest(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
    learning_id: str,
) -> str:
    cursor = await db.execute(
        "SELECT id,title,description,prevention,project,invalid_at,superseded_by "
        "FROM learnings WHERE id=? AND workspace_id=?",
        (learning_id, workspace_id),
    )
    row = await cursor.fetchone()
    if row is None or row["project"] != project_slug:
        raise NotFoundError(code="learning_not_found", message="Learning was not found")
    if row["invalid_at"] is not None or row["superseded_by"] is not None:
        raise ConflictError(
            code="learning_not_live",
            message="Historical pointer requires a live learning",
        )
    substantive = {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "prevention": str(row["prevention"] or ""),
    }
    return _canonical_digest(substantive)


async def create_historical_pointer(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    source_project_slug: str,
    source_kind: str,
    source_ref: str,
    expected_source_body_digest: str,
    relation: str,
    target_project_slug: str,
    operation_id: str,
    idempotency_key: str,
    expected_cloud_f_epoch: int,
    lease_expires_at: datetime,
    target_decision_id: str | None = None,
    target_relative_path: str | None = None,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Add a forward/applies-to pointer without rewriting the source artifact."""
    require_role_ctx(ctx, "admin", "super_admin")
    await _require_approval_authority(ctx, db)
    workspace_id = require_workspace_ctx(ctx)
    actor = ctx.user_id or ctx.username
    if source_kind not in {"decision", "handoff", "learning"}:
        raise ValidationError(code="invalid_source_kind", message="Unsupported source kind")
    if relation not in {"forward", "applies_to"}:
        raise ValidationError(code="invalid_pointer_relation", message="Unsupported pointer relation")
    if source_kind == "learning" and relation != "applies_to":
        raise ValidationError(
            code="invalid_pointer_relation",
            message="Learning lineage must use applies_to",
        )
    if source_kind == "handoff" and relation != "forward":
        raise ValidationError(
            code="invalid_pointer_relation",
            message="Handoff lineage must use forward",
        )
    op_id = _validate_token("operation_id", operation_id, _OPERATION_ID_RE)
    idempotency_key = _validate_idempotency_key(idempotency_key)
    expected_body_digest = _validate_digest(
        "expected_source_body_digest", expected_source_body_digest
    )
    if source_kind == "learning":
        source_rel = _validate_token("learning_id", source_ref, _DECISION_ID_RE)
    else:
        source_rel = _validate_relative_path(source_ref, require_markdown=False)

    target_id: str | None = None
    requested_target_rel: str | None = None
    if target_decision_id is not None:
        target_id = _validate_token("decision_id", target_decision_id, _DECISION_ID_RE)
        if target_relative_path is not None:
            requested_target_rel = _validate_relative_path(
                target_relative_path,
                require_markdown=True,
            )
    else:
        if relation == "forward":
            raise ValidationError(
                code="pointer_target_required",
                message="Forward pointer requires an accepted decision target",
            )
        if target_relative_path is not None:
            requested_target_rel = _validate_relative_path(
                target_relative_path,
                require_markdown=False,
            )

    replay_request = {
        "source_project_slug": source_project_slug,
        "source_kind": source_kind,
        "source_ref": source_rel,
        "expected_source_body_digest": expected_body_digest,
        "relation": relation,
        "target_project_slug": target_project_slug,
        "target_decision_id": target_id,
        "expected_cloud_f_epoch": expected_cloud_f_epoch,
    }
    if requested_target_rel is not None:
        replay_request["target_relative_path"] = requested_target_rel

    row = await _operation_row(
        db,
        workspace_id=workspace_id,
        operation_id=op_id,
        idempotency_key=idempotency_key,
    )
    completed = _completed_operation_replay(
        row,
        actor=actor,
        operation_id=op_id,
        idempotency_key=idempotency_key,
        operation_kind="pointer",
        primary_project_slug=source_project_slug,
        expected_cloud_f_epoch=expected_cloud_f_epoch,
        expected_request=replay_request,
    )
    if completed is not None:
        return completed

    project_dir = project_lifecycle.project_directory(
        source_project_slug,
        projects_root=projects_root,
    )
    async with project_lifecycle.async_project_mutation_guard(
        projects_root=project_dir.parent
    ):
        row = await _operation_row(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            idempotency_key=idempotency_key,
        )
        completed = _completed_operation_replay(
            row,
            actor=actor,
            operation_id=op_id,
            idempotency_key=idempotency_key,
            operation_kind="pointer",
            primary_project_slug=source_project_slug,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            expected_request=replay_request,
        )
        if completed is not None:
            return completed

        if source_kind == "learning":
            live_body_digest = await _learning_body_digest(
                db,
                workspace_id=workspace_id,
                project_slug=source_project_slug,
                learning_id=source_rel,
            )
        else:
            source_path, source_rel, _source_dir = _artifact_path(
                source_project_slug,
                source_rel,
                projects_root=project_dir.parent,
                require_markdown=False,
            )
            live_body_digest = _parse_document(source_path, source_rel).body_digest
        if live_body_digest != expected_body_digest:
            raise ConflictError(
                code="artifact_body_digest_changed",
                message="Historical artifact body digest changed",
            )

        target_rel: str | None = None
        if target_id is not None:
            cursor = await db.execute(
                "SELECT project_slug,relative_path,lifecycle FROM governed_decisions "
                "WHERE workspace_id=? AND decision_id=?",
                (workspace_id, target_id),
            )
            target = await cursor.fetchone()
            if (
                target is None
                or target["project_slug"] != target_project_slug
                or target["lifecycle"] != "accepted"
            ):
                raise ConflictError(
                    code="replacement_decision_not_accepted",
                    message="Pointer target decision must exist and be accepted",
                )
            target_rel = str(target["relative_path"])
            if requested_target_rel is not None and requested_target_rel != target_rel:
                raise ConflictError(
                    code="pointer_target_mismatch",
                    message="Pointer target path does not match the decision",
                )
        else:
            await project_lifecycle.ensure_project_lifecycle(
                db,
                workspace_id=workspace_id,
                project_slug=target_project_slug,
                projects_root=project_dir.parent,
            )
            target_rel = requested_target_rel

        payload = {
            **replay_request,
            "target_relative_path": target_rel,
        }
        request_digest = _canonical_digest(payload)
        if row is not None:
            _verify_existing_operation(
                row,
                actor=actor,
                operation_id=op_id,
                idempotency_key=idempotency_key,
                operation_kind="pointer",
                primary_project_slug=source_project_slug,
                request_digest=request_digest,
                expected_cloud_f_epoch=expected_cloud_f_epoch,
            )
        row = await _start_or_resume_operation(
            ctx,
            db,
            row=row,
            operation_id=op_id,
            idempotency_key=idempotency_key,
            operation_kind="pointer",
            primary_project_slug=source_project_slug,
            affected_projects={source_project_slug: source_rel},
            request_payload=payload,
            request_digest=request_digest,
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            lease_expires_at=lease_expires_at,
            projects_root=project_dir.parent,
        )
        if row["state"] == "prepared":
            # This saga step is intentionally metadata-only. Revalidate the
            # immutable source before advancing the persisted step marker.
            if source_kind == "learning":
                current_digest = await _learning_body_digest(
                    db,
                    workspace_id=workspace_id,
                    project_slug=source_project_slug,
                    learning_id=source_rel,
                )
            else:
                source_path, _rel, _dir = _artifact_path(
                    source_project_slug,
                    source_rel,
                    projects_root=project_dir.parent,
                    require_markdown=False,
                )
                current_digest = _parse_document(source_path, source_rel).body_digest
            if current_digest != expected_body_digest:
                raise ConflictError(
                    code="artifact_body_digest_changed",
                    message="Historical artifact changed after pointer preparation",
                )
            await _mark_filesystem_applied(
                db,
                workspace_id=workspace_id,
                operation_id=op_id,
            )
        try:
            await db.execute(
                "INSERT INTO historical_artifact_pointers "
                "(workspace_id,operation_id,source_project_slug,source_kind,"
                "source_relative_path,source_body_digest,relation,target_project_slug,"
                "target_decision_id,target_relative_path,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    op_id,
                    source_project_slug,
                    source_kind,
                    source_rel,
                    expected_body_digest,
                    relation,
                    target_project_slug,
                    target_id,
                    target_rel,
                    actor,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(
                code="historical_pointer_exists",
                message="Historical pointer already exists",
            ) from exc
        result = {
            "operation_id": op_id,
            "source_project_slug": source_project_slug,
            "source_kind": source_kind,
            "source_ref": source_rel,
            "source_body_digest": expected_body_digest,
            "relation": relation,
            "target_project_slug": target_project_slug,
            "target_decision_id": target_id,
            "target_relative_path": target_rel,
            "previous_cloud_f_epoch": expected_cloud_f_epoch,
            "cloud_f_change_epoch": expected_cloud_f_epoch + 1,
        }
        return await _complete_operation(
            db,
            workspace_id=workspace_id,
            operation_id=op_id,
            actor=actor,
            affected_projects=[source_project_slug],
            expected_cloud_f_epoch=expected_cloud_f_epoch,
            result=result,
        )


async def read_decision(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
    decision_id: str,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Direct read remains available for a known decision in an archived project."""
    identifier = _validate_token("decision_id", decision_id, _DECISION_ID_RE)
    cursor = await db.execute(
        "SELECT * FROM governed_decisions WHERE workspace_id=? AND project_slug=? "
        "AND decision_id=?",
        (workspace_id, project_slug, identifier),
    )
    row = await cursor.fetchone()
    if row is None:
        raise NotFoundError(code="decision_not_found", message="Decision was not found")
    path, rel_path, _project_dir = _artifact_path(
        project_slug,
        str(row["relative_path"]),
        projects_root=projects_root,
        require_markdown=True,
    )
    snapshot = _parse_document(path, rel_path)
    if (
        snapshot.content_digest != row["content_digest"]
        or snapshot.body_digest != row["body_digest"]
    ):
        raise ConflictError(
            code="decision_content_drift",
            message="Decision file no longer matches its governed record",
        )
    pointers = await list_historical_pointers(
        db,
        workspace_id=workspace_id,
        source_project_slug=project_slug,
        source_kind="decision",
        source_ref=rel_path,
    )
    result = {key: row[key] for key in row.keys()}
    result.update(
        {
            "frontmatter": snapshot.frontmatter,
            "body": snapshot.body.decode("utf-8"),
            "pointers": pointers,
        }
    )
    return result


async def list_historical_pointers(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    source_project_slug: str,
    source_kind: str | None = None,
    source_ref: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["workspace_id=?", "source_project_slug=?"]
    params: list[Any] = [workspace_id, source_project_slug]
    if source_kind is not None:
        clauses.append("source_kind=?")
        params.append(source_kind)
    if source_ref is not None:
        clauses.append("source_relative_path=?")
        params.append(source_ref)
    cursor = await db.execute(
        "SELECT workspace_id,operation_id,source_project_slug,source_kind,"
        "source_relative_path,source_body_digest,relation,target_project_slug,"
        "target_decision_id,target_relative_path,created_by,created_at "
        "FROM historical_artifact_pointers WHERE " + " AND ".join(clauses) +
        " ORDER BY created_at,operation_id",
        tuple(params),
    )
    return [{key: row[key] for key in row.keys()} for row in await cursor.fetchall()]


async def read_decision_operation(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    operation_id: str,
) -> dict[str, Any]:
    cursor = await db.execute(
        "SELECT * FROM decision_lifecycle_operations "
        "WHERE workspace_id=? AND operation_id=?",
        (workspace_id, operation_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise NotFoundError(
            code="decision_operation_not_found",
            message="Decision operation was not found",
        )
    result = {key: row[key] for key in row.keys()}
    result["request"] = json.loads(result.pop("request_json"))
    result["result"] = (
        json.loads(result.pop("result_json")) if result.get("result_json") else None
    )
    return result


__all__ = [
    "accept_decision",
    "create_decision",
    "create_historical_pointer",
    "list_historical_pointers",
    "read_decision",
    "read_decision_operation",
    "supersede_decision",
]
