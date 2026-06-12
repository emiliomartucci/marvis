# v1.0.0 - 2026-05-27 - S1 F1.10: ingest_triage use_cases extracted from router
"""Ingest-triage use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

The router (``routers/ingest_triage.py``) becomes a thin adapter that resolves
identity into a :class:`CallerContext`, resolves visibility (the ``UserInfo.teams``
expansion stays at the transport boundary, like the learnings visibility
template), reads ``UploadFile``/``Request``, and maps :class:`ServiceError` ->
``HTTPException`` via ``routers/_adapter.to_http``. The Python MCP surface (later)
calls the SAME functions with ``CallerContext.local_single_user()``.

Four adapter responsibilities (the same split every S1 router applies):

1. VISIBILITY resolution at the adapter, enforcement in the use_case.
   ``get_visible_projects`` needs ``UserInfo.teams`` (not carried by
   ``CallerContext`` by design). The adapter resolves ``visible_projects`` and
   passes it as a keyword arg; this module only ENFORCES it. Enforcement mirrors
   the legacy ``check_project_access``: a not-visible slug raises
   ``NotFoundError`` (404, not 403 — does not reveal existence). List paths
   receive the set and scope the query. ``None`` means "unrestricted"
   (admin/agent-bypass, or the local/MCP surface).

2. SLUG/PATH-VALIDATION guards with bespoke dict bodies stay in the ADAPTER.
   ``_project_input_root`` + the patch path-containment / collision checks raise
   ``HTTPException`` with structured ``{"error": "INVALID_SLUG_FORMAT", ...}``
   bodies that existing tests pin byte-for-byte. They live at the
   filesystem/transport boundary; the use_case receives already-resolved,
   already-validated ``Path`` objects and bare values. Net: the use_case raises
   only clean ``ServiceError`` subclasses for state/RBAC/not-found.

3. MULTIPART / ``UploadFile`` handling is an ADAPTER concern. The adapter reads
   the upload (chunked, size-guarded), materialises bytes onto disk, and hands
   the resulting ``Path`` list to the use_case. The use_case never touches
   ``UploadFile``/``Request``/``Form``.

4. FIRE-AND-FORGET scheduling + SSE broadcast stay in the ADAPTER. The use_case
   performs the durable DB transition (the source of truth) and returns; the
   adapter schedules ``parse_pending``/``execute_saga`` (``asyncio.create_task``)
   and emits the ``broadcast_ingest_changed`` websocket notification. Both are
   best-effort transport side-channels, not domain truth.

Filesystem side-effects that ARE domain I/O — the ``patch`` atomic file move
(copy-to-staging -> rename -> unlink source) and the ``delete`` containment-checked
unlink — stay IN this module: they mutate the durable project tree alongside the
DB row and must succeed/fail together with it.

Service imports (``broadcast_ingest_changed``, ``log_skip``, ``parse_pending``,
``execute_saga``, ``dispatch``) are done FUNCTION-LOCAL: some of them (e.g.
``parser_router``, ``dispatch``) transitively import a module that imports
``fastapi``; importing them lazily keeps THIS module fastapi-free at import time
(the property the import-linter contract + the smoke test assert).
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal

# A resolver ``project_slug -> resolved project root Path``. The adapter binds
# this to the patchable router-level ``PROJECTS_ROOT`` so the containment base
# path used by filesystem-domain operations stays test-monkeypatchable.
ProjectRootResolver = Callable[[str], Path]

import aiosqlite
from pydantic import BaseModel

from core.api.use_cases._context import CallerContext, require_role_ctx
from core.api.use_cases._errors import (
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# ServiceError subclass that carries a bespoke legacy HTTP detail body.
#
# A handful of patch-path failures return structured ``detail`` dicts (e.g.
# ``{"error": "target_sha_collision", ...}``) or plain strings that existing
# tests pin byte-for-byte. ``DetailedServiceError`` carries that exact body in
# ``legacy_detail`` + an explicit ``http_status``; the HTTP adapter, when it sees
# ``legacy_detail``, builds ``HTTPException(status, detail=legacy_detail)`` so the
# response is byte-identical to the legacy router. Other surfaces (MCP) ignore
# ``legacy_detail`` and fall back to ``code``/``message`` like any ServiceError.
# ---------------------------------------------------------------------------


class DetailedServiceError(ServiceError):
    """ServiceError carrying an explicit ``http_status`` + legacy ``detail`` body."""

    def __init__(self, *, code: str, message: str, http_status: int, detail: Any) -> None:
        super().__init__(code=code, message=message)
        self.http_status = http_status
        self.legacy_detail = detail

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

# Reject Windows absolute-drive prefixes in upload-relative paths (path safety).
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")

PendingStatus = Literal[
    "queued",
    "parser_waiting",
    "parsing",
    "classified",
    "awaiting_triage",
    "approved",
    "inserted",
    "done",
    "parse_error",
    "rejected",
]

SkippedReason = Literal[
    "dedup_sha256",
    "invalid_path",
    "mime_not_allowed",
    "parse_error_pre_dispatch",
]

HistoryDecisionFilter = Literal[
    "all",
    "auto_approved",
    "auto_rejected",
    "manual_approved",
    "manual_rejected",
    "parse_error",
    "skipped",
]

IngestHistoryDecision = Literal[
    "auto_approved",
    "auto_rejected",
    "manual_approved",
    "manual_rejected",
    "parse_error",
    "skipped",
]


# ---------------------------------------------------------------------------
# Domain DTOs (Pydantic is allowed in use_cases — only ``fastapi`` is forbidden)
# ---------------------------------------------------------------------------


class IngestPendingItem(BaseModel):
    id: str
    file_path: str
    project_slug: str
    source_kind: str
    mime_type: str | None
    file_size_bytes: int | None
    parser_used: str | None
    extracted_text: str | None
    structure: dict[str, Any] | None
    classification: dict[str, Any] | None
    status: str
    error_message: str | None
    target_folder: str | None
    target_filename: str | None
    created_at: str
    updated_at: str


class IngestDecisionResponse(BaseModel):
    id: str
    status: str


class IngestUploadSkipped(BaseModel):
    path: str
    reason: str


class IngestUploadDedup(BaseModel):
    """UX-6: one file silently deduplicated against an existing pending row.

    Frontend uses this list to show "dedup" status (yellow icon) in the
    upload modal instead of a misleading "done" green tick.
    """

    path: str
    existing_ingest_id: str


class IngestSkipEntry(BaseModel):
    """UX-6: row from ingest_skipped (mig 103) — populates "Ignorati" sidebar."""

    id: str
    file_path_attempted: str
    project_slug: str
    sha256: str | None = None
    reason: SkippedReason
    existing_ingest_id: str | None = None
    error_message: str | None = None
    created_at: str
    created_by: str | None = None


class IngestHistoryEntry(BaseModel):
    """Read-only audit row for the ingest decision history drawer."""

    id: str
    source: Literal["ingest_pending", "ingest_skipped"]
    decision: IngestHistoryDecision
    status: str
    file_path: str
    filename: str
    project_slug: str
    mime_type: str | None = None
    file_size_bytes: int | None = None
    parser_used: str | None = None
    document_type: str | None = None
    confidence: float | None = None
    target_folder: str | None = None
    target_filename: str | None = None
    reason: str | None = None
    triage_decision_id: str | None = None
    existing_ingest_id: str | None = None
    created_at: str
    updated_at: str


class IngestUploadResponse(BaseModel):
    project_slug: str
    uploaded_files: int
    queued_items: int
    skipped_files: list[IngestUploadSkipped]
    dedup_files: list[IngestUploadDedup] = []


class IngestPendingPatch(BaseModel):
    """E4 patch body — currently supports project_slug only.

    target_folder/target_filename will be added in a follow-up E4 iteration once
    the audit table is wired through the UI.
    """

    project_slug: str | None = None


class IngestReparseBatchResponse(BaseModel):
    queued_count: int
    status: str


class IngestPreflightResponse(BaseModel):
    """UX-1: pre-upload dedup probe. Frontend calls this before POST /upload-folder
    to detect SHA256 collision against any non-rejected row in the target project.
    Returns enough metadata for the user to choose ignore/replace/rename."""

    exists: bool
    id: str | None = None
    status: str | None = None
    file_path: str | None = None


# ---------------------------------------------------------------------------
# Pure helpers — JSON / formatting
# ---------------------------------------------------------------------------


def _json_obj(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _row_to_item(row: aiosqlite.Row) -> IngestPendingItem:
    return IngestPendingItem(
        id=row["id"],
        file_path=row["file_path"],
        project_slug=row["project_slug"],
        source_kind=row["source_kind"],
        mime_type=row["mime_type"],
        file_size_bytes=row["file_size_bytes"],
        parser_used=row["parser_used"],
        extracted_text=row["extracted_text"],
        structure=_json_obj(row["structure_json"]),
        classification=_json_obj(row["classification_json"]),
        status=row["status"],
        error_message=row["error_message"],
        target_folder=row["target_folder"],
        target_filename=row["target_filename"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _basename(path: str | None) -> str:
    if not path:
        return "-"
    name = PurePosixPath(path).name
    return name or path


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first_numeric(*values: Any) -> float | None:
    for value in values:
        numeric = _numeric(value)
        if numeric is not None:
            return numeric
    return None


def _pending_history_decision(
    row: aiosqlite.Row,
    classification: dict[str, Any] | None,
) -> IngestHistoryDecision:
    status = row["status"]
    triage_decision_id = row["triage_decision_id"] or ""
    llm = classification.get("llm_metadata") if classification else None
    llm_obj = llm if isinstance(llm, dict) else {}

    if status == "parse_error":
        return "parse_error"
    if status == "rejected":
        if triage_decision_id.startswith("auto_reject:") or llm_obj.get("auto_rejected"):
            return "auto_rejected"
        return "manual_rejected"
    if (
        triage_decision_id.startswith("auto_approve:")
        or bool(classification and classification.get("auto_approve"))
        or bool(llm_obj.get("auto_approved"))
    ):
        return "auto_approved"
    return "manual_approved"


def _pending_history_entry(row: aiosqlite.Row) -> IngestHistoryEntry:
    classification = _json_obj(row["classification_json"])
    llm = classification.get("llm_metadata") if classification else None
    llm_obj = llm if isinstance(llm, dict) else {}
    decision = _pending_history_decision(row, classification)
    confidence = _first_numeric(
        llm_obj.get("composite_confidence"),
        llm_obj.get("confidence"),
        llm_obj.get("llm_confidence"),
        classification.get("confidence") if classification else None,
    )
    document_type = (
        llm_obj.get("document_type")
        or (classification.get("type") if classification else None)
    )
    reason = (
        row["triage_decision_id"]
        or llm_obj.get("auto_reject_reason")
        or llm_obj.get("reason")
        or (classification.get("reason") if classification else None)
        or row["error_message"]
    )
    return IngestHistoryEntry(
        id=row["id"],
        source="ingest_pending",
        decision=decision,
        status=row["status"],
        file_path=row["file_path"],
        filename=_basename(row["file_path"]),
        project_slug=row["project_slug"],
        mime_type=row["mime_type"],
        file_size_bytes=row["file_size_bytes"],
        parser_used=row["parser_used"],
        document_type=str(document_type) if document_type else None,
        confidence=confidence,
        target_folder=row["target_folder"],
        target_filename=row["target_filename"],
        reason=str(reason) if reason else None,
        triage_decision_id=row["triage_decision_id"],
        existing_ingest_id=(
            str(llm_obj["existing_ingest_id"])
            if llm_obj.get("existing_ingest_id")
            else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _skipped_history_entry(row: aiosqlite.Row) -> IngestHistoryEntry:
    return IngestHistoryEntry(
        id=row["id"],
        source="ingest_skipped",
        decision="skipped",
        status="skipped",
        file_path=row["file_path_attempted"],
        filename=_basename(row["file_path_attempted"]),
        project_slug=row["project_slug"],
        reason=row["reason"],
        existing_ingest_id=row["existing_ingest_id"],
        created_at=row["created_at"],
        updated_at=row["created_at"],
    )


def _ingest_pending_file_exists(file_path: str | None) -> bool:
    if not file_path:
        return False
    try:
        return Path(file_path).exists()
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Pure helpers — slug / path
# ---------------------------------------------------------------------------


def _safe_upload_relative_path(filename: str | None) -> PurePosixPath | None:
    normalized = (filename or "").replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or _WINDOWS_DRIVE_RE.match(normalized)
    ):
        return None
    rel_path = PurePosixPath(normalized)
    parts = [part for part in rel_path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return PurePosixPath(*parts)


def _safe_target(input_root: Path, relative_path: PurePosixPath) -> Path | None:
    target = (input_root / Path(*relative_path.parts)).resolve()
    if not target.is_relative_to(input_root):
        return None
    return target


def _available_target(input_root: Path, relative_path: PurePosixPath) -> Path | None:
    target = _safe_target(input_root, relative_path)
    if target is None or not target.exists():
        return target

    suffix = target.suffix
    stem = target.stem
    for _ in range(10):
        candidate = target.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")
        if not candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Row loading + visibility enforcement
# ---------------------------------------------------------------------------


def _enforce_visibility(project_slug: str, visible_projects: set[str] | None) -> None:
    """Mirror of ``check_project_access``: 404 (not 403) — do not reveal existence.

    ``visible_projects=None`` means unrestricted (admin/agent-bypass or
    local/MCP surface). The adapter resolves the set via
    ``get_visible_projects``; this only enforces it.
    """
    if visible_projects is not None and project_slug not in visible_projects:
        raise NotFoundError(code="not_found", message="Not found")


async def _load_row(db: aiosqlite.Connection, ingest_id: str) -> aiosqlite.Row:
    async with db.execute(
        "SELECT * FROM ingest_pending WHERE id = ?", (ingest_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise NotFoundError(code="ingest_not_found", message="Ingest item not found")
    return row


async def _visible_row(
    db: aiosqlite.Connection,
    ingest_id: str,
    visible_projects: set[str] | None,
) -> aiosqlite.Row:
    row = await _load_row(db, ingest_id)
    _enforce_visibility(row["project_slug"], visible_projects)
    return row


# ---------------------------------------------------------------------------
# Use cases — reads
# ---------------------------------------------------------------------------


async def list_pending(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    status: PendingStatus | None = None,
    project_slug: str | None = None,
    limit: int = 100,
    visible_projects: set[str] | None = None,
    project_filter_visible: bool = True,
) -> list[IngestPendingItem]:
    """List ingest_pending rows (operator+).

    Visibility: when ``project_slug`` is given the adapter has already verified
    access (and passes ``project_filter_visible``); otherwise ``visible_projects``
    scopes the query. ``parse_error`` rows whose file no longer exists are hidden
    when no explicit status filter is set (parity with the legacy router).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if project_slug:
        if not project_filter_visible:
            raise NotFoundError(code="not_found", message="Not found")
        clauses.append("project_slug = ?")
        params.append(project_slug)
    elif visible_projects is not None:
        if not visible_projects:
            return []
        placeholders = ",".join("?" for _ in visible_projects)
        clauses.append(f"project_slug IN ({placeholders})")
        params.extend(sorted(visible_projects))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    async with db.execute(
        f"""
        SELECT *
          FROM ingest_pending
          {where}
         ORDER BY created_at DESC
         LIMIT ?
        """,
        params,
    ) as cursor:
        rows = await cursor.fetchall()

    if status is None:
        rows = [
            row
            for row in rows
            if not (
                row["status"] == "parse_error"
                and not _ingest_pending_file_exists(row["file_path"])
            )
        ]

    return [_row_to_item(row) for row in rows]


async def list_skipped(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str | None = None,
    reason: SkippedReason | None = None,
    limit: int = 100,
    visible_projects: set[str] | None = None,
    project_filter_visible: bool = True,
) -> list[IngestSkipEntry]:
    """UX-6: list rows from ingest_skipped (audit log) for the sidebar (operator+)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    clauses: list[str] = []
    params: list[Any] = []
    if reason:
        clauses.append("reason = ?")
        params.append(reason)
    if project_slug:
        if not project_filter_visible:
            raise NotFoundError(code="not_found", message="Not found")
        clauses.append("project_slug = ?")
        params.append(project_slug)
    elif visible_projects is not None:
        if not visible_projects:
            return []
        placeholders = ",".join("?" for _ in visible_projects)
        clauses.append(f"project_slug IN ({placeholders})")
        params.extend(sorted(visible_projects))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    async with db.execute(
        f"""
        SELECT id, file_path_attempted, project_slug, sha256, reason,
               existing_ingest_id, error_message, created_at, created_by
          FROM ingest_skipped
          {where}
         ORDER BY created_at DESC
         LIMIT ?
        """,
        params,
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        IngestSkipEntry(
            id=row["id"],
            file_path_attempted=row["file_path_attempted"],
            project_slug=row["project_slug"],
            sha256=row["sha256"],
            reason=row["reason"],
            existing_ingest_id=row["existing_ingest_id"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )
        for row in rows
    ]


async def list_history(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    decision: HistoryDecisionFilter = "all",
    today: bool = True,
    project_slug: str | None = None,
    limit: int = 80,
    visible_projects: set[str] | None = None,
    project_filter_visible: bool = True,
) -> list[IngestHistoryEntry]:
    """Read-only decision history for the Ingest Auto drawer (operator+)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    pending_clauses: list[str] = [
        "status IN ('done', 'inserted', 'rejected', 'parse_error')"
    ]
    skipped_clauses: list[str] = []
    pending_params: list[Any] = []
    skipped_params: list[Any] = []

    if project_slug:
        if not project_filter_visible:
            raise NotFoundError(code="not_found", message="Not found")
        pending_clauses.append("project_slug = ?")
        skipped_clauses.append("project_slug = ?")
        pending_params.append(project_slug)
        skipped_params.append(project_slug)
    elif visible_projects is not None:
        if not visible_projects:
            return []
        placeholders = ",".join("?" for _ in visible_projects)
        pending_clauses.append(f"project_slug IN ({placeholders})")
        skipped_clauses.append(f"project_slug IN ({placeholders})")
        sorted_projects = sorted(visible_projects)
        pending_params.extend(sorted_projects)
        skipped_params.extend(sorted_projects)

    if today:
        pending_clauses.append(
            "date(updated_at, '+2 hours') = date('now', '+2 hours')"
        )
        skipped_clauses.append(
            "date(created_at, '+2 hours') = date('now', '+2 hours')"
        )

    entries: list[IngestHistoryEntry] = []
    fetch_limit = min(limit * 3, 750)

    if decision != "skipped":
        pending_params.append(fetch_limit)
        async with db.execute(
            f"""
            SELECT *
              FROM ingest_pending
             WHERE {' AND '.join(pending_clauses)}
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            pending_params,
        ) as cursor:
            pending_rows = await cursor.fetchall()
        for row in pending_rows:
            entry = _pending_history_entry(row)
            if decision == "all" or entry.decision == decision:
                entries.append(entry)

    if decision in {"all", "skipped"}:
        skipped_params.append(fetch_limit)
        skipped_where = (
            f"WHERE {' AND '.join(skipped_clauses)}" if skipped_clauses else ""
        )
        async with db.execute(
            f"""
            SELECT id, file_path_attempted, project_slug, sha256, reason,
                   existing_ingest_id, error_message, created_at, created_by
              FROM ingest_skipped
              {skipped_where}
             ORDER BY created_at DESC
             LIMIT ?
            """,
            skipped_params,
        ) as cursor:
            skipped_rows = await cursor.fetchall()
        entries.extend(_skipped_history_entry(row) for row in skipped_rows)

    entries.sort(key=lambda item: item.updated_at, reverse=True)
    return entries[:limit]


async def counters(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    today: bool = True,
    visible_projects: set[str] | None = None,
) -> dict[str, int]:
    """Counter auto vs manual approvals con SUM aggregato server-side (H-D11)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    clauses: list[str] = ["status IN ('done','inserted')"]
    params: list[Any] = []

    if visible_projects is not None:
        if not visible_projects:
            return {"auto": 0, "manual": 0}
        placeholders = ",".join("?" for _ in visible_projects)
        clauses.append(f"project_slug IN ({placeholders})")
        params.extend(sorted(visible_projects))

    if today:
        clauses.append("date(updated_at, '+2 hours') = date('now', '+2 hours')")

    where = "WHERE " + " AND ".join(clauses)
    async with db.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN json_extract(classification_json, '$.auto_approve')=1 THEN 1 ELSE 0 END), 0) AS auto_count,
            COALESCE(SUM(CASE WHEN json_extract(classification_json, '$.auto_approve') IS NOT 1 THEN 1 ELSE 0 END), 0) AS manual_count
          FROM ingest_pending
          {where}
        """,
        params,
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return {"auto": 0, "manual": 0}
    return {
        "auto": int(row["auto_count"] or 0),
        "manual": int(row["manual_count"] or 0),
    }


async def preflight(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    sha256: str,
    project_slug: str,
    project_visible: bool = True,
) -> IngestPreflightResponse:
    """Pre-upload dedup check against any non-rejected row (operator+)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    if not project_visible:
        raise NotFoundError(code="not_found", message="Not found")

    async with db.execute(
        """
        SELECT id, status, file_path
          FROM ingest_pending
         WHERE sha256 = ?
           AND project_slug = ?
           AND status != 'rejected'
         LIMIT 1
        """,
        (sha256, project_slug),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return IngestPreflightResponse(exists=False)
    return IngestPreflightResponse(
        exists=True,
        id=row["id"],
        status=row["status"],
        file_path=row["file_path"],
    )


# ---------------------------------------------------------------------------
# Use cases — state transitions (durable DB writes; scheduling stays in adapter)
# ---------------------------------------------------------------------------


async def retry_parse(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    ingest_id: str,
    visible_projects: set[str] | None = None,
) -> IngestDecisionResponse:
    """Validate a row is parseable. Adapter schedules ``parse_pending`` after.

    No DB write — the legacy endpoint only checks state then fire-and-forgets.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    row = await _visible_row(db, ingest_id, visible_projects)
    if row["status"] not in {"queued", "parse_error"}:
        raise ConflictError(code="not_parseable", message="Item is not parseable")
    return IngestDecisionResponse(id=ingest_id, status="parser_waiting")


async def approve(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    ingest_id: str,
    visible_projects: set[str] | None = None,
) -> tuple[IngestDecisionResponse, str]:
    """Approve an awaiting_triage row. Adapter schedules the saga + broadcasts.

    Returns ``(response, project_slug)`` so the adapter can emit the SSE event
    and schedule ``execute_saga`` without re-loading the row.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    row = await _visible_row(db, ingest_id, visible_projects)
    if not row["target_folder"] or not row["target_filename"]:
        raise ConflictError(
            code="no_target_classification",
            message="Item has no target classification",
        )

    cursor = await db.execute(
        """
        UPDATE ingest_pending
           SET status = 'approved',
               triage_decision_id = ?,
               updated_at = datetime('now')
         WHERE id = ?
           AND status = 'awaiting_triage'
        """,
        (f"approve:{ctx.user_id}", ingest_id),
    )
    if cursor.rowcount != 1:
        raise ConflictError(
            code="not_awaiting_triage",
            message="Item is no longer awaiting triage",
        )
    await db.commit()
    return IngestDecisionResponse(id=ingest_id, status="approved"), row["project_slug"]


async def reject(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    ingest_id: str,
    visible_projects: set[str] | None = None,
) -> tuple[IngestDecisionResponse, str]:
    """Reject a row (soft state transition). Adapter broadcasts the SSE event.

    Returns ``(response, project_slug)``.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    row = await _visible_row(db, ingest_id, visible_projects)
    cursor = await db.execute(
        """
        UPDATE ingest_pending
           SET status = 'rejected',
               triage_decision_id = ?,
               updated_at = datetime('now')
         WHERE id = ?
           AND status IN (
               'queued', 'parser_waiting', 'parsing', 'classified',
               'awaiting_triage', 'parse_error'
           )
        """,
        (f"reject:{ctx.user_id}", ingest_id),
    )
    if cursor.rowcount != 1:
        raise ConflictError(code="cannot_reject", message="Item cannot be rejected")
    await db.commit()
    return IngestDecisionResponse(id=ingest_id, status="rejected"), row["project_slug"]


async def delete(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    ingest_id: str,
    project_root_for: ProjectRootResolver,
    visible_projects: set[str] | None = None,
) -> str:
    """Hard-delete a row + containment-checked unlink of physical files (operator+).

    Filesystem unlink is DOMAIN I/O (mutates the project tree alongside the DB
    row), so it stays here. The adapter passes ``project_root_for`` — a resolver
    bound to the patchable router-level ``PROJECTS_ROOT`` — so the containment
    base path stays test-monkeypatchable. Returns ``project_slug`` for the
    adapter's broadcast.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    row = await _visible_row(db, ingest_id, visible_projects)
    project_slug = row["project_slug"]
    project_root = project_root_for(project_slug)

    deleted_paths: list[str] = []

    def _safe_unlink(candidate: Path) -> None:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            return
        if not resolved.is_relative_to(project_root):
            return
        if resolved.exists() and resolved.is_file():
            resolved.unlink(missing_ok=True)
            deleted_paths.append(str(resolved))

    if row["file_path"]:
        _safe_unlink(Path(row["file_path"]))

    target_folder = row["target_folder"]
    target_filename = row["target_filename"]
    if target_folder and target_filename:
        target = project_root / target_folder / target_filename
        _safe_unlink(target)

    await db.execute("DELETE FROM ingest_pending WHERE id = ?", (ingest_id,))
    await db.commit()
    return project_slug


async def classify_force(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    ingest_id: str,
    visible_projects: set[str] | None = None,
) -> IngestDecisionResponse:
    """Force re-trigger the parser/classifier (operator+). Adapter schedules parse.

    Resets the row to ``queued`` (parse_pending guard only accepts
    queued|parse_error) so the adapter's scheduled ``parse_pending`` re-runs.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    row = await _visible_row(db, ingest_id, visible_projects)
    if row["status"] not in {"queued", "parse_error", "awaiting_triage", "classified"}:
        raise ConflictError(
            code="invalid_state_for_classify",
            message="invalid_state_for_classify",
        )
    await db.execute(
        """
        UPDATE ingest_pending
           SET status = 'queued',
               error_message = NULL,
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (ingest_id,),
    )
    await db.commit()
    return IngestDecisionResponse(id=ingest_id, status="parser_waiting")


async def reparse_single(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    ingest_id: str,
    visible_projects: set[str] | None = None,
) -> IngestDecisionResponse:
    """Re-parse a single row without changing project_slug (operator+)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    row = await _visible_row(db, ingest_id, visible_projects)
    if row["status"] not in {"parse_error", "awaiting_triage"}:
        raise ValidationError(
            code="invalid_state_for_reparse",
            message="invalid_state_for_reparse",
        )
    await db.execute(
        """
        UPDATE ingest_pending
           SET status = 'queued',
               error_message = NULL,
               updated_at = datetime('now')
         WHERE id = ?
        """,
        (ingest_id,),
    )
    await db.commit()
    return IngestDecisionResponse(id=ingest_id, status="parser_waiting")


async def reparse_batch(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    target_status: str = "parse_error",
    visible_projects: set[str] | None = None,
) -> tuple[IngestReparseBatchResponse, list[str]]:
    """Select all rows with ``target_status`` for re-parse (admin+).

    Returns ``(response, ids)`` so the adapter can fan out
    ``asyncio.create_task(parse_pending(id))`` per row.
    """
    require_role_ctx(ctx, "admin", "super_admin")
    clauses = ["status = ?"]
    params: list[Any] = [target_status]
    if visible_projects is not None:
        if not visible_projects:
            return IngestReparseBatchResponse(queued_count=0, status=target_status), []
        placeholders = ",".join("?" for _ in visible_projects)
        clauses.append(f"project_slug IN ({placeholders})")
        params.extend(sorted(visible_projects))
    where = " AND ".join(clauses)
    async with db.execute(
        f"SELECT id FROM ingest_pending WHERE {where}",
        params,
    ) as cursor:
        rows = await cursor.fetchall()
    ids = [row["id"] for row in rows]
    return IngestReparseBatchResponse(queued_count=len(ids), status=target_status), ids


# ---------------------------------------------------------------------------
# Use case — patch (project_slug change + atomic file move; domain I/O)
# ---------------------------------------------------------------------------


async def patch_pending(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    row: aiosqlite.Row,
    new_slug: str,
    new_input_root: Path,
    projects_root: Path,
    changed_by: str,
    source_ip: str | None,
    user_agent: str | None,
) -> IngestPendingItem:
    """E4 — change ``project_slug`` post-upload with an atomic file move (operator+).

    The adapter has already: loaded + visibility-checked the row, run the
    If-Match optimistic-lock check, verified the row is in an allowed state,
    validated the destination slug format, verified destination visibility,
    resolved ``new_input_root``, and passed the patchable ``projects_root`` (the
    slug/path guards with bespoke dict bodies stay at the transport boundary).
    This function owns the domain truth: the
    sha collision check, the source/target containment checks, the
    copy-to-staging -> rename -> unlink-source move (M-D11 TOCTOU-safe), the row
    UPDATE, and the change-history row — all committed together.

    Raises ``DetailedServiceError`` for the patch-path domain failures so the
    adapter reproduces the exact legacy ``detail`` bodies tests pin.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    ingest_id = row["id"]

    async with db.execute(
        """
        SELECT id, status, file_path
          FROM ingest_pending
         WHERE sha256 = ?
           AND project_slug = ?
           AND id != ?
         LIMIT 1
        """,
        (row["sha256"], new_slug, ingest_id),
    ) as cursor:
        duplicate = await cursor.fetchone()
    if duplicate is not None:
        raise DetailedServiceError(
            code="target_sha_collision",
            message=(
                "A file with the same content already exists in the "
                f"target project '{new_slug}'"
            ),
            http_status=409,
            detail={
                "error": "target_sha_collision",
                "message": (
                    "A file with the same content already exists in the "
                    f"target project '{new_slug}'"
                ),
                "project_slug": new_slug,
                "existing_ingest_id": duplicate["id"],
                "existing_status": duplicate["status"],
                "existing_file_path": duplicate["file_path"],
                "sha256": row["sha256"],
            },
        )

    old_path = Path(row["file_path"]).resolve()
    if not old_path.is_relative_to(projects_root):
        raise DetailedServiceError(
            code="path_integrity_violation",
            message="path_integrity_violation",
            http_status=500,
            detail="path_integrity_violation",
        )
    if not old_path.exists():
        raise DetailedServiceError(
            code="source_file_missing",
            message="Original upload no longer exists on disk",
            http_status=409,
            detail={
                "error": "source_file_missing",
                "message": "Original upload no longer exists on disk",
            },
        )

    new_path = (new_input_root / old_path.name).resolve()
    if not new_path.is_relative_to(new_input_root):
        raise DetailedServiceError(
            code="target_path_escape",
            message="target_path_escape",
            http_status=400,
            detail="target_path_escape",
        )
    if new_path.exists():
        raise DetailedServiceError(
            code="target_filename_collision",
            message=(
                f"File '{old_path.name}' already exists in project "
                f"'{new_slug}/input'"
            ),
            http_status=409,
            detail={
                "error": "target_filename_collision",
                "message": (
                    f"File '{old_path.name}' already exists in project "
                    f"'{new_slug}/input'"
                ),
            },
        )

    # M-D11 TOCTOU-safe: copy to staging on the same filesystem as the
    # destination, then atomic rename. Rollback the staging file on any DB
    # error before we touch the source.
    staging_dir = new_input_root.parent / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / f"{uuid.uuid4().hex}-{old_path.name}"

    await asyncio.to_thread(shutil.copy2, str(old_path), str(staging))

    try:
        await db.execute(
            """
            UPDATE ingest_pending
               SET project_slug = ?,
                   file_path = ?,
                   updated_at = strftime('%Y-%m-%d %H:%M:%f','now')
             WHERE id = ?
            """,
            (new_slug, str(new_path), ingest_id),
        )

        await db.execute(
            """
            INSERT INTO ingest_change_history
                (ingest_pending_id, field_name, old_value, new_value,
                 changed_by, source_ip, user_agent)
            VALUES (?, 'project_slug', ?, ?, ?, ?, ?)
            """,
            (
                ingest_id,
                row["project_slug"],
                new_slug,
                changed_by,
                source_ip,
                user_agent,
            ),
        )

        await db.commit()
    except sqlite3.IntegrityError as exc:
        staging.unlink(missing_ok=True)
        if "ingest_pending.sha256, ingest_pending.project_slug" in str(exc):
            raise DetailedServiceError(
                code="target_sha_collision",
                message=(
                    "A file with the same content already exists in the "
                    f"target project '{new_slug}'"
                ),
                http_status=409,
                detail={
                    "error": "target_sha_collision",
                    "message": (
                        "A file with the same content already exists in the "
                        f"target project '{new_slug}'"
                    ),
                    "project_slug": new_slug,
                    "sha256": row["sha256"],
                },
            ) from exc
        raise
    except Exception:
        staging.unlink(missing_ok=True)
        raise

    # Promote staging -> final destination, then unlink the source.
    try:
        await asyncio.to_thread(staging.rename, new_path)
    except Exception as exc:
        staging.unlink(missing_ok=True)
        raise DetailedServiceError(
            code="rename_failed",
            message="DB updated but file move failed; manual recovery needed",
            http_status=500,
            detail={
                "error": "rename_failed",
                "message": "DB updated but file move failed; manual recovery needed",
            },
        ) from exc
    await asyncio.to_thread(old_path.unlink, True)

    refreshed = await _load_row(db, ingest_id)
    return _row_to_item(refreshed)
