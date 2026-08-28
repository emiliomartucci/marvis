# v2.0.0 - 2026-05-27 - S1 F1.10: thin adapter over use_cases.ingest_triage
"""HTTP adapter for the ingest-triage domain (S1 collapse-runtime).

This router is a thin transport adapter. The list/transition/patch/delete
CRUD + RBAC + visibility-enforcement + filesystem-domain-I/O logic lives in
:mod:`core.api.use_cases.ingest_triage` (pure, fastapi-free). Each handler
resolves identity into a :class:`CallerContext`, resolves visibility at the
boundary (``get_visible_projects`` / ``check_project_access`` need
``UserInfo.teams``), calls the use_case, and maps :class:`ServiceError` ->
``HTTPException`` via :func:`_to_http`.

STAYS IN THE ADAPTER (transport concerns):
  * SLUG/PATH guards with bespoke dict bodies (``_project_input_root``,
    ``_project_root``) — they raise ``HTTPException`` with structured
    ``{"error": ...}`` bodies pinned by ``test_ingest_upload_validation`` /
    ``test_ingest_patch_endpoint``. The use_case receives resolved ``Path``s.
  * MULTIPART / ``UploadFile`` reading (``_save_upload_file``,
    ``_save_upload_to_path``, zip extraction) — the adapter materialises bytes
    onto disk and hands ``Path`` lists to the use_case (which never touches
    ``UploadFile``/``Request``/``Form``).
  * The governed-ingress endpoint ``POST /`` (``ingest_unified``) stays whole:
    it is transport to the bone (Request body parsing, idempotency ``Response``
    replay, rate-limit ``JSONResponse`` with ``Retry-After`` headers, the
    ``@limiter`` decorator) and is NOT part of the MCP surface.
  * ``preview_ingest`` (``FileResponse`` / ``PlainTextResponse``) — pure
    transport.
  * FIRE-AND-FORGET scheduling (``asyncio.create_task(parse_pending/execute_saga)``)
    + the ``broadcast_ingest_changed`` SSE notification — best-effort transport
    side-channels. The use_case performs the durable DB transition (truth) and
    returns; the adapter schedules + broadcasts.

DELEGATES TO THE USE_CASE (domain truth):
  list_pending / list_skipped / list_history / counters / preflight / retry_parse
  / approve / reject / delete / patch_pending / classify_force / reparse_single
  / reparse_batch — including the patch atomic file-move + the delete unlink
  (domain filesystem I/O committed alongside the DB row).

The moved DTOs + pure helpers are re-exported from the use_case so (a) the
``response_model=`` below references the same classes and (b) any importer keeps
working unchanged. ``PROJECTS_ROOT`` + the path-resolution helpers stay defined
HERE so the filesystem tests can ``monkeypatch.setattr(ingest_router,
"PROJECTS_ROOT", tmp_path)``; the adapter resolves roots against this value and
passes the resolved Path (or a resolver bound to it) into the use_case.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal

import aiofiles
import aiosqlite
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from core.api.db import acquire_db, get_db, get_write_db, write_db
from core.api.models import IngestIngressResponse, IngestJsonPayload, UserInfo
from core.api.rate_limit import limiter
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.routers._browser_mutation_denial import agent_only_route
from core.api.services.ingest.api_key_auth import IngestKeyContext, require_ingest_key
from core.api.services.ingest.dispatch import IngestProvenance, dispatch_files_batched
from core.api.services.ingest.events import broadcast_ingest_changed
from core.api.services.ingest.ignore_patterns import MAX_FILE_SIZE_BYTES, should_ignore
from core.api.services.ingest.ingress import (
    MAX_IDEMPOTENCY_KEY_LEN,
    MAX_INGRESS_FILE_COUNT,
    MAX_JSON_BODY_BYTES,
    check_and_increment_quota,
    check_and_increment_rate,
    claim_idempotency,
    claim_webhook_nonce,
    decode_json_content,
    finalize_idempotency,
    parse_webhook_headers,
    json_request_fingerprint,
    multipart_request_fingerprint,
    release_idempotency,
    seconds_to_midnight_utc,
    seconds_to_next_minute,
    verify_webhook_signature,
    webhook_project_scope,
    webhook_workspace_id,
)
from core.api.services.ingest.insert_saga import execute_saga
from core.api.services.ingest.parser_router import parse_pending
from core.api.services.ingest.parsers.zip_unpacker import ZipContainerError, safe_extract_zip
from core.api.services.ingest.skip_log import SkipReason, log_skip
from core.api.services.ingest.watcher import (
    ProjectWorkspaceOwnershipError,
    require_unique_project_workspace,
)
from core.api.use_cases import ingest_triage as uc
from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import ServiceError
from core.api.visibility import check_project_access, get_visible_projects

# Re-export the moved DTOs + pure helpers + constants from the use_case so that
# (a) `response_model=` below references the same classes and (b) existing
# importers keep working unchanged.
from core.api.use_cases.ingest_triage import (  # noqa: F401  (re-export surface)
    IngestDecisionResponse,
    IngestHistoryDecision,
    IngestHistoryEntry,
    IngestPendingItem,
    IngestPendingPatch,
    IngestPreflightResponse,
    IngestReparseBatchResponse,
    IngestSkipEntry,
    IngestUploadDedup,
    IngestUploadResponse,
    IngestUploadSkipped,
    _available_target,
    _basename,
    _first_numeric,
    _json_obj,
    _numeric,
    _pending_history_decision,
    _pending_history_entry,
    _row_to_item,
    _safe_target,
    _safe_upload_relative_path,
    _skipped_history_entry,
)

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])

# Module-level constant kept HERE (not re-exported) so the filesystem-path tests
# can ``monkeypatch.setattr(ingest_router, "PROJECTS_ROOT", tmp_path)``. The
# adapter resolves project roots against this value and passes the resolved Path
# (or a resolver bound to it) into the use_case, keeping the patch seam intact.
PROJECTS_ROOT = Path(os.environ.get("MARVIS_PROJECTS_ROOT") or os.environ.get("WORKSPACE_ROOT") or "/data/projects").resolve()
INGEST_UPLOAD_TMP = Path("/tmp/pir-ingest-uploads")
UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_CONTAINER_UPLOAD_BYTES = 512 * 1024 * 1024
_UNKNOWN_UPLOAD_MIME_TYPES = {"", "application/octet-stream"}
_PARSEABLE_UPLOAD_MIME_TYPES = {
    "application/mp4",
    "application/ogg",
    "application/pdf",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "audio/x-m4a",
    "text/markdown",
    "text/plain",
}
_PARSEABLE_UPLOAD_MIME_PREFIXES = ("audio/", "image/", "text/", "video/")
# H-D16 — slug whitelist for upload targets. Allows `&` for legacy slugs
# (es: `c&i-normativa`). Path-traversal sequences are rejected by both the
# regex and the post-resolve containment check.
_PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_&\-]{0,127}$")


# ---------------------------------------------------------------------------
# Adapter helpers — error mapping + identity
# ---------------------------------------------------------------------------


def _to_http(err: ServiceError) -> HTTPException:
    """Map a domain ServiceError to HTTPException, honouring legacy detail bodies.

    ``DetailedServiceError`` (patch path) carries a bespoke ``legacy_detail`` body
    (dict or plain string) + explicit ``http_status`` that existing tests pin;
    use it verbatim. All other ServiceErrors go through the shared ``to_http``
    ({code, message} structured body).
    """
    legacy = getattr(err, "legacy_detail", None)
    if legacy is not None:
        return HTTPException(status_code=err.http_status, detail=legacy)
    return to_http(err)


def _ctx(user: UserInfo, *, is_human_session: bool = False) -> CallerContext:
    return CallerContext.from_user_info(user, is_human_session=is_human_session)


async def _require_ingest_project_owner(
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    workspace_id: str,
) -> None:
    """Map the shared exact-owner gate to a non-enumerating HTTP 404."""
    try:
        await require_unique_project_workspace(
            db,
            project_slug=project_slug,
            workspace_id=workspace_id,
        )
    except ProjectWorkspaceOwnershipError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


# ---------------------------------------------------------------------------
# Adapter helpers — slug / path guards (bespoke dict bodies, pinned by tests)
# ---------------------------------------------------------------------------


def _project_root(project_slug: str) -> Path:
    return (PROJECTS_ROOT / project_slug).resolve()


def _row_current_path(row: aiosqlite.Row) -> Path:
    root = _project_root(row["project_slug"])
    if row["target_folder"] and row["target_filename"]:
        target = (root / row["target_folder"] / row["target_filename"]).resolve()
        if target.exists():
            return target
    return Path(row["file_path"]).resolve()


def _validate_project_slug_format(project_slug: str) -> None:
    # H-D16 — slug regex whitelist (defense in depth before path resolution).
    if not _PROJECT_SLUG_RE.fullmatch(project_slug):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_SLUG_FORMAT",
                "message": (
                    f"Project slug '{project_slug}' must match [a-z0-9][a-z0-9_&-]+"
                ),
            },
        )


def _project_input_root(project_slug: str) -> Path:
    _validate_project_slug_format(project_slug)
    project_root = _project_root(project_slug)
    # Containment check — even with regex, refuse anything that escapes
    # PROJECTS_ROOT after symlink resolution.
    if not project_root.is_relative_to(PROJECTS_ROOT):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "SLUG_PATH_TRAVERSAL",
                "message": f"Resolved path for '{project_slug}' escapes projects root",
            },
        )
    if not project_root.is_dir():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "PROJECT_NOT_FOUND",
                "message": f"Project '{project_slug}' not found",
            },
        )
    input_root = (project_root / "input").resolve()
    if not input_root.is_relative_to(project_root):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "SLUG_PATH_TRAVERSAL",
                "message": "Input directory escapes project root",
            },
        )
    if not input_root.exists():
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INPUT_DIR_MISSING",
                "message": f"Project '{project_slug}' has no input/ directory",
            },
        )
    return input_root


def _assert_project_path(row: aiosqlite.Row) -> Path:
    path = _row_current_path(row)
    root = _project_root(row["project_slug"])
    if not path.is_relative_to(root):
        raise HTTPException(status_code=404, detail="Not found")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


# ---------------------------------------------------------------------------
# Adapter helpers — row loaders (UserInfo-based visibility, 404 not 403)
# ---------------------------------------------------------------------------


async def _visible_row(
    db: aiosqlite.Connection,
    ingest_id: str,
    user: UserInfo,
) -> aiosqlite.Row:
    workspace_id = require_workspace_ctx(_ctx(user))
    async with db.execute(
        "SELECT * FROM ingest_pending WHERE workspace_id = ? AND id = ?",
        (workspace_id, ingest_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Ingest item not found")
    await check_project_access(row["project_slug"], user, db)
    return row


async def _project_visible(
    db: aiosqlite.Connection,
    user: UserInfo,
    project_slug: str,
) -> bool:
    """Resolve whether ``project_slug`` is visible to the caller.

    Mirrors ``check_project_access`` semantics but returns a bool (passed into
    the use_case as ``project_filter_visible``) so the use_case stays free of
    ``UserInfo`` / the visibility cache.
    """
    visible = await get_visible_projects(db, user)
    return visible is None or project_slug in visible


# ---------------------------------------------------------------------------
# Adapter helpers — upload / MIME (transport: reads UploadFile)
# ---------------------------------------------------------------------------


def _normalized_upload_mime(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _is_parseable_upload_mime(mime_type: str) -> bool:
    if mime_type in _UNKNOWN_UPLOAD_MIME_TYPES:
        return True
    return mime_type in _PARSEABLE_UPLOAD_MIME_TYPES or any(
        mime_type.startswith(prefix) for prefix in _PARSEABLE_UPLOAD_MIME_PREFIXES
    )


async def _save_upload_file(
    upload: UploadFile,
    input_root: Path,
    relative_path: PurePosixPath,
) -> tuple[Path | None, str | None]:
    ignore_reason = should_ignore(relative_path)
    if ignore_reason:
        return None, ignore_reason

    target = _available_target(input_root, relative_path)
    if target is None:
        return None, "invalid-path"
    target.parent.mkdir(parents=True, exist_ok=True)

    total_size = 0
    async with aiofiles.open(target, "wb") as f:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_FILE_SIZE_BYTES:
                await f.close()
                target.unlink(missing_ok=True)
                return None, "file-too-large"
            await f.write(chunk)

    return target, None


async def _save_upload_to_path(
    upload: UploadFile,
    target: Path,
    *,
    max_bytes: int = MAX_CONTAINER_UPLOAD_BYTES,
) -> int:
    total_size = 0
    async with aiofiles.open(target, "wb") as f:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > max_bytes:
                await f.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="container-too-large")
            await f.write(chunk)
    return total_size


# ---------------------------------------------------------------------------
# Upload endpoints (multipart — transport, materialises bytes then dispatches)
# ---------------------------------------------------------------------------


@agent_only_route(router, "/upload-folder", methods=["POST"], response_model=IngestUploadResponse)
async def upload_ingest_folder(
    project_slug: str = Form(...),
    files: list[UploadFile] = File(...),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> IngestUploadResponse:
    # Syntax is not project-existence evidence and is safe to reject before
    # visibility.  Valid slugs still pass the 404 access gate before any
    # filesystem existence check.
    _validate_project_slug_format(project_slug)
    await check_project_access(project_slug, user, db)
    workspace_id = require_workspace_ctx(_ctx(user))
    await _require_ingest_project_owner(
        db,
        project_slug=project_slug,
        workspace_id=workspace_id,
    )
    input_root = _project_input_root(project_slug)

    saved_paths: list[Path] = []
    skipped: list[IngestUploadSkipped] = []
    pending_skip_logs: list[tuple[str, SkipReason, str | None]] = []
    for upload in files:
        relative_path = _safe_upload_relative_path(upload.filename)
        if relative_path is None:
            attempted = upload.filename or "unnamed"
            skipped.append(IngestUploadSkipped(path=attempted, reason="invalid-path"))
            pending_skip_logs.append((attempted, "invalid_path", None))
            continue

        upload_mime = _normalized_upload_mime(upload.content_type)
        if not _is_parseable_upload_mime(upload_mime):
            display_mime = upload_mime or "unknown"
            skipped.append(
                IngestUploadSkipped(
                    path=str(relative_path),
                    reason=f"mime_not_supported:{display_mime}",
                )
            )
            pending_skip_logs.append(
                (
                    str(relative_path),
                    "mime_not_allowed",
                    f"unsupported MIME: {display_mime}",
                )
            )
            continue

        saved_path, skip_reason = await _save_upload_file(
            upload,
            input_root,
            relative_path,
        )
        if saved_path is None:
            skipped.append(
                IngestUploadSkipped(
                    path=str(relative_path),
                    reason=skip_reason or "skipped",
                )
            )
            # UX-6: classify the skip into the audit enum. The save helper
            # rejects MIME-disallowed extensions, oversize files, and a few
            # corner cases — collapse them all to mime_not_allowed since the
            # user-visible distinction is "we won't ingest this", not the
            # internal reason. invalid_path is reserved for path traversal
            # which is handled in the branch above.
            pending_skip_logs.append(
                (str(relative_path), "mime_not_allowed", skip_reason)
            )
            continue
        saved_paths.append(saved_path)

    if pending_skip_logs:
        async with write_db(label="ingest.upload_folder.skip_log") as skip_db:
            for path, reason, error_message in pending_skip_logs:
                await log_skip(
                    skip_db,
                    workspace_id=workspace_id,
                    file_path_attempted=path,
                    project_slug=project_slug,
                    reason=reason,
                    error_message=error_message,
                    created_by=user.user_id,
                )

    dispatch = await dispatch_files_batched(
        saved_paths,
        workspace_id=workspace_id,
        projects_root=PROJECTS_ROOT,
        source_kind="manual_upload",
    )
    return IngestUploadResponse(
        project_slug=project_slug,
        uploaded_files=len(saved_paths),
        queued_items=dispatch.queued_count,
        skipped_files=skipped,
        dedup_files=[
            IngestUploadDedup(
                path=item.file_path,
                existing_ingest_id=item.existing_ingest_id,
            )
            for item in dispatch.dedup_files
        ],
    )




async def handle_signed_webhook_request(request: Request) -> IngestIngressResponse:
    """Signed webhook ingress for trusted C&I sources (U8)."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=400, detail="Content-Type must be application/json.")

    try:
        webhook_source, timestamp, nonce, signature = parse_webhook_headers(request.headers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > MAX_JSON_BODY_BYTES:
        raise HTTPException(status_code=413, detail="JSON body too large.")
    raw = await request.body()
    if len(raw) > MAX_JSON_BODY_BYTES:
        raise HTTPException(status_code=413, detail="JSON body too large.")

    try:
        workspace_id = webhook_workspace_id()
        request_sha256 = verify_webhook_signature(raw, timestamp=timestamp, signature=signature)
    except ServiceError as exc:
        raise to_http(exc)
    claimed = await claim_webhook_nonce(
        workspace_id,
        webhook_source,
        nonce,
        request_sha256,
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="Webhook nonce was already used.")

    try:
        payload = IngestJsonPayload.model_validate_json(raw)
    except PydanticValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc.errors()}")

    allowed_projects = webhook_project_scope()
    if not allowed_projects:
        raise HTTPException(status_code=401, detail="Webhook project scope is not configured.")
    if payload.project not in allowed_projects:
        raise HTTPException(status_code=422, detail="Project is not in this webhook's scope.")

    async with acquire_db() as owner_db:
        await _require_ingest_project_owner(
            owner_db,
            project_slug=payload.project,
            workspace_id=workspace_id,
        )

    try:
        data, filename = decode_json_content(payload.content)
    except ServiceError as exc:
        raise to_http(exc)

    input_root = _project_input_root(payload.project)
    relative_path = _safe_upload_relative_path(filename)
    if relative_path is None:
        raise HTTPException(status_code=422, detail="content.filename is unsafe (path traversal / absolute path).")
    target = _available_target(input_root, relative_path)
    if target is None:
        raise HTTPException(status_code=422, detail="content.filename is invalid.")
    target.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(target, "wb") as f:
        await f.write(data)

    source = payload.source or webhook_source
    provenance = IngestProvenance(
        source_kind="webhook_ingress",
        api_key_id=None,
        source=source,
        ingest_policy="trusted",
        metadata=payload.metadata,
    )
    dispatch = await dispatch_files_batched(
        [target],
        workspace_id=workspace_id,
        projects_root=PROJECTS_ROOT,
        provenance=provenance,
    )
    return IngestIngressResponse(
        project=payload.project,
        source=source,
        policy="trusted",
        dry_run=False,
        would_route="awaiting_triage",
        queued_items=dispatch.queued_count,
        dedup_items=len(dispatch.dedup_files),
        skipped_items=0,
    )


@router.post("/webhook", response_model=IngestIngressResponse, status_code=202)
async def ingest_signed_webhook(request: Request) -> IngestIngressResponse:
    return await handle_signed_webhook_request(request)

@router.post("", response_model=IngestIngressResponse)
@limiter.limit("120/minute")
async def ingest_unified(
    request: Request,
    dry_run: bool = Query(False),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: IngestKeyContext = Depends(require_ingest_key),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Unified governed ingress (M1 CAPTURE U2).

    Accepts a multipart file upload OR an application/json payload, authenticated
    by an ingestion API key (Bearer). In M1 every api_ingress item lands in
    awaiting_triage (per-source auto-insert is U3). Abuse controls: per-key
    per-minute rate limit + daily quota (429 + Retry-After), Idempotency-Key
    (claim-first), and a side-effect-free dry_run.

    Stays whole in the adapter: transport to the bone (Request body parsing,
    idempotency Response replay, rate-limit JSONResponse with headers, the
    @limiter decorator). Not part of the MCP surface.
    """
    content_type = (
        (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    )

    payload: IngestJsonPayload | None = None
    uploads: list[StarletteUploadFile] = []

    # --- 1. Parse by content type, resolve target project + source label ---
    if content_type == "application/json":
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > MAX_JSON_BODY_BYTES:
            raise HTTPException(status_code=413, detail="JSON body too large.")
        raw = await request.body()
        if len(raw) > MAX_JSON_BODY_BYTES:
            raise HTTPException(status_code=413, detail="JSON body too large.")
        try:
            payload = IngestJsonPayload.model_validate_json(raw)
        except PydanticValidationError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON payload: {exc.errors()}")
        project = payload.project
        source = payload.source or ctx.default_source
    elif content_type == "multipart/form-data":
        form = await request.form()
        project_raw = form.get("project") or form.get("project_slug")
        if not project_raw:
            raise HTTPException(
                status_code=422,
                detail="multipart ingest requires a 'project' form field.",
            )
        project = str(project_raw)
        source_raw = form.get("source")
        source = (str(source_raw) if source_raw else None) or ctx.default_source
        for key in ("files", "file"):
            for value in form.getlist(key):
                if isinstance(value, StarletteUploadFile):
                    uploads.append(value)
        if not uploads:
            raise HTTPException(
                status_code=422,
                detail="multipart ingest requires at least one 'files' upload.",
            )
    else:
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be application/json or multipart/form-data.",
        )

    # --- 2. Scope enforcement: key-bound, default-deny, no slug enumeration ---
    if project not in ctx.project_scope:
        raise HTTPException(
            status_code=403,
            detail="Project not in this key's scope.",
        )

    await _require_ingest_project_owner(
        db,
        project_slug=project,
        workspace_id=ctx.workspace_id,
    )

    policy = ctx.ingest_policy

    # --- 3. dry_run: validate everything, zero side effects ---
    if dry_run:
        if payload is not None:
            decode_json_content(payload.content)  # raises on url / bad content
        return IngestIngressResponse(
            project=project,
            source=source,
            policy=policy,
            dry_run=True,
            would_route="awaiting_triage",
        )

    # --- 4. Idempotency claim (FIRST write — prevents duplicate rows) ---
    if idempotency_key is not None:
        if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"Idempotency-Key must be 1..{MAX_IDEMPOTENCY_KEY_LEN} chars.",
            )
        if payload is not None:
            fingerprint = json_request_fingerprint(payload)
        else:
            fingerprint = multipart_request_fingerprint(
                project, [(u.filename or "", u.size or 0) for u in uploads]
            )
        state, stored = await claim_idempotency(ctx.key_id, idempotency_key, fingerprint)
        if state == "replay":
            return Response(content=stored or "{}", media_type="application/json")
        if state == "pending":
            raise HTTPException(
                status_code=409,
                detail="A request with this Idempotency-Key is still in flight; retry shortly.",
            )
        if state == "mismatch":
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key was already used with a different payload.",
            )
        # state == "claimed" -> proceed

    try:
        # --- 5. Rate limit (per-key, per-minute, durable) ---
        # Return JSONResponse directly (not raise): the global HTTPException
        # handler rebuilds the response and drops exc.headers, which would lose
        # Retry-After (R6). Release the idempotency claim first since no
        # exception propagates to the except block below.
        if not await check_and_increment_rate(ctx.key_id, ctx.rate_limit_per_min):
            if idempotency_key is not None:
                await release_idempotency(ctx.key_id, idempotency_key)
            return JSONResponse(
                status_code=429,
                content={"detail": "Per-minute rate limit exceeded for this key."},
                headers={"Retry-After": str(seconds_to_next_minute())},
            )
        # --- 6. Daily quota ---
        if not await check_and_increment_quota(ctx.key_id, ctx.daily_quota):
            if idempotency_key is not None:
                await release_idempotency(ctx.key_id, idempotency_key)
            return JSONResponse(
                status_code=429,
                content={"detail": "Daily quota exhausted for this key."},
                headers={"Retry-After": str(seconds_to_midnight_utc())},
            )

        input_root = _project_input_root(project)

        # --- 7. Materialize content into <project>/input/ (path guards) ---
        saved_paths: list[Path] = []
        skipped = 0
        if payload is not None:
            data, filename = decode_json_content(payload.content)
            relative_path = _safe_upload_relative_path(filename)
            if relative_path is None:
                raise HTTPException(
                    status_code=422,
                    detail="content.filename is unsafe (path traversal / absolute path).",
                )
            target = _available_target(input_root, relative_path)
            if target is None:
                raise HTTPException(status_code=422, detail="content.filename is invalid.")
            target.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(target, "wb") as f:
                await f.write(data)
            saved_paths.append(target)
        else:
            if len(uploads) > MAX_INGRESS_FILE_COUNT:
                raise HTTPException(
                    status_code=413,
                    detail=f"Too many files (max {MAX_INGRESS_FILE_COUNT} per request).",
                )
            for upload in uploads:
                relative_path = _safe_upload_relative_path(upload.filename)
                if relative_path is None:
                    skipped += 1
                    continue
                saved_path, _reason = await _save_upload_file(
                    upload, input_root, relative_path
                )
                if saved_path is None:
                    skipped += 1
                    continue
                saved_paths.append(saved_path)

        # --- 8. Dispatch into the existing pipeline. The key policy snapshot
        # rides on the row; per-source enforcement (open -> triage, trusted ->
        # auto-insert-eligible) happens in parse_pending. payload.metadata is
        # persisted to ingest_pending.ingress_metadata and reconciled into
        # structure_json by the parser (U3).
        provenance = IngestProvenance(
            source_kind="api_ingress",
            api_key_id=ctx.key_id,
            source=source,
            ingest_policy=policy,
            metadata=payload.metadata if payload is not None else None,
        )
        dispatch = await dispatch_files_batched(
            saved_paths,
            workspace_id=ctx.workspace_id,
            projects_root=PROJECTS_ROOT,
            provenance=provenance,
        )

        response = IngestIngressResponse(
            project=project,
            source=source,
            policy=policy,
            dry_run=False,
            would_route="awaiting_triage",
            queued_items=dispatch.queued_count,
            dedup_items=len(dispatch.dedup_files),
            skipped_items=skipped,
        )
        if idempotency_key is not None:
            await finalize_idempotency(
                ctx.key_id, idempotency_key, response.model_dump_json()
            )
        return response
    except Exception:
        # Release the claim so a failed request does not block retries (the
        # rate/quota counters already consumed are an intentional abuse cost).
        if idempotency_key is not None:
            await release_idempotency(ctx.key_id, idempotency_key)
        raise


@agent_only_route(router, "/upload-zip", methods=["POST"], response_model=IngestUploadResponse)
async def upload_ingest_zip(
    project_slug: str = Form(...),
    archive: UploadFile = File(...),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> IngestUploadResponse:
    _validate_project_slug_format(project_slug)
    await check_project_access(project_slug, user, db)
    workspace_id = require_workspace_ctx(_ctx(user))
    await _require_ingest_project_owner(
        db,
        project_slug=project_slug,
        workspace_id=workspace_id,
    )
    input_root = _project_input_root(project_slug)
    archive_name = PurePosixPath(
        (archive.filename or "upload.zip").replace("\\", "/")
    ).name
    if not archive_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="invalid-zip")

    INGEST_UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="ingest-zip-", dir=INGEST_UPLOAD_TMP))
    zip_path = temp_root / archive_name
    extract_root = temp_root / "extract"
    try:
        await _save_upload_to_path(archive, zip_path)
        extract_result = await asyncio.to_thread(
            safe_extract_zip, zip_path, extract_root
        )

        saved_paths: list[Path] = []
        skipped = [
            IngestUploadSkipped(path=item.path, reason=item.reason)
            for item in extract_result.skipped
        ]
        for item in extract_result.files:
            target = _available_target(input_root, item.relative_path)
            if target is None:
                skipped.append(
                    IngestUploadSkipped(
                        path=str(item.relative_path),
                        reason="invalid-path",
                    )
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(item.path), str(target))
            saved_paths.append(target)

        dispatch = await dispatch_files_batched(
            saved_paths,
            workspace_id=workspace_id,
            projects_root=PROJECTS_ROOT,
            source_kind="manual_upload",
        )
        return IngestUploadResponse(
            project_slug=project_slug,
            uploaded_files=len(saved_paths),
            queued_items=dispatch.queued_count,
            skipped_files=skipped,
        )
    except ZipContainerError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    finally:
        await asyncio.to_thread(shutil.rmtree, temp_root, True)


# ---------------------------------------------------------------------------
# Read endpoints — delegate to use_case (visibility resolved here)
# ---------------------------------------------------------------------------


@router.get("/counters")
async def get_triage_counters(
    today: bool = Query(default=True),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, int]:
    """Counter auto vs manual approvals con SUM aggregato server-side (H-D11)."""
    visible_projects = await get_visible_projects(db, user)
    try:
        return await uc.counters(
            _ctx(user), db, today=today, visible_projects=visible_projects
        )
    except ServiceError as err:
        raise _to_http(err)


@router.get("/history", response_model=list[IngestHistoryEntry])
async def list_ingest_history(
    decision: uc.HistoryDecisionFilter = Query(default="all"),
    today: bool = Query(default=True),
    project_slug: str | None = Query(default=None),
    limit: int = Query(default=80, ge=1, le=250),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[IngestHistoryEntry]:
    """Read-only decision history for the Ingest Auto drawer."""
    visible_projects: set[str] | None = None
    project_visible = True
    if project_slug:
        project_visible = await _project_visible(db, user, project_slug)
    else:
        visible_projects = await get_visible_projects(db, user)
    try:
        return await uc.list_history(
            _ctx(user),
            db,
            decision=decision,
            today=today,
            project_slug=project_slug,
            limit=limit,
            visible_projects=visible_projects,
            project_filter_visible=project_visible,
        )
    except ServiceError as err:
        raise _to_http(err)


@router.get("/pending", response_model=list[IngestPendingItem])
async def list_pending_ingest(
    status: uc.PendingStatus | None = Query(default=None),
    project_slug: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=250),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[IngestPendingItem]:
    visible_projects: set[str] | None = None
    project_visible = True
    if project_slug:
        project_visible = await _project_visible(db, user, project_slug)
    else:
        visible_projects = await get_visible_projects(db, user)
    try:
        return await uc.list_pending(
            _ctx(user),
            db,
            status=status,
            project_slug=project_slug,
            limit=limit,
            visible_projects=visible_projects,
            project_filter_visible=project_visible,
        )
    except ServiceError as err:
        raise _to_http(err)


@router.get("/skipped", response_model=list[IngestSkipEntry])
async def list_ingest_skipped(
    project_slug: str | None = Query(default=None),
    reason: uc.SkippedReason | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=250),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[IngestSkipEntry]:
    """UX-6: list rows from ingest_skipped (audit log) for the sidebar."""
    visible_projects: set[str] | None = None
    project_visible = True
    if project_slug:
        project_visible = await _project_visible(db, user, project_slug)
    else:
        visible_projects = await get_visible_projects(db, user)
    try:
        return await uc.list_skipped(
            _ctx(user),
            db,
            project_slug=project_slug,
            reason=reason,
            limit=limit,
            visible_projects=visible_projects,
            project_filter_visible=project_visible,
        )
    except ServiceError as err:
        raise _to_http(err)


@router.get("/preflight", response_model=IngestPreflightResponse)
async def preflight_ingest(
    sha256: str = Query(..., min_length=64, max_length=64),
    project_slug: str = Query(..., min_length=2, max_length=64),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> IngestPreflightResponse:
    """Pre-upload dedup check. Returns the existing non-rejected row if one
    already exists for `(sha256, project_slug)`. Rejected rows are intentionally
    excluded — the re-upload flow re-activates them via `enqueue_file` (fix4)."""
    project_visible = await _project_visible(db, user, project_slug)
    try:
        return await uc.preflight(
            _ctx(user),
            db,
            sha256=sha256,
            project_slug=project_slug,
            project_visible=project_visible,
        )
    except ServiceError as err:
        raise _to_http(err)


# ---------------------------------------------------------------------------
# Transition endpoints — use_case owns the durable DB write; adapter schedules
# the background work + emits the SSE broadcast.
# ---------------------------------------------------------------------------


@agent_only_route(router, "/pending/{ingest_id}/parse", methods=["POST"], response_model=IngestDecisionResponse)
async def retry_parse_ingest(
    ingest_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> IngestDecisionResponse:
    visible_projects = await get_visible_projects(db, user)
    try:
        result = await uc.retry_parse(
            _ctx(user), db, ingest_id=ingest_id, visible_projects=visible_projects
        )
    except ServiceError as err:
        raise _to_http(err)
    asyncio.create_task(parse_pending(ingest_id, user.workspace_id))
    return result


@agent_only_route(router, "/pending/{ingest_id}/approve", methods=["POST"], response_model=IngestDecisionResponse)
async def approve_ingest(
    ingest_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> IngestDecisionResponse:
    visible_projects = await get_visible_projects(db, user)
    try:
        result, project_slug = await uc.approve(
            _ctx(user), db, ingest_id=ingest_id, visible_projects=visible_projects
        )
    except ServiceError as err:
        raise _to_http(err)
    asyncio.create_task(execute_saga(ingest_id, user.workspace_id))
    await broadcast_ingest_changed(
        "approved",
        workspace_id=user.workspace_id,
        ingest_id=ingest_id,
        project_slug=project_slug,
        status="approved",
    )
    return result


@agent_only_route(router, "/pending/{ingest_id}/reject", methods=["POST"], response_model=IngestDecisionResponse)
async def reject_ingest(
    ingest_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> IngestDecisionResponse:
    visible_projects = await get_visible_projects(db, user)
    try:
        result, project_slug = await uc.reject(
            _ctx(user), db, ingest_id=ingest_id, visible_projects=visible_projects
        )
    except ServiceError as err:
        raise _to_http(err)
    await broadcast_ingest_changed(
        "rejected",
        workspace_id=user.workspace_id,
        ingest_id=ingest_id,
        project_slug=project_slug,
        status="rejected",
    )
    return result


@agent_only_route(router, "/pending/{ingest_id}", methods=["DELETE"], status_code=204)
async def delete_ingest(
    ingest_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> Response:
    """Hard-delete an ingest_pending row + cleanup file fisici associati.

    Removes both the source file (row.file_path) and the saga target
    (project_root/target_folder/target_filename) when they exist. All
    paths are containment-checked against the row's project_root before
    unlink — refuses to touch anything outside.

    Use case: re-test an upload after an LLM/saga bug fix. State machine
    (UPDATE→rejected) keeps the row for audit; this endpoint nukes it.
    """
    visible_projects = await get_visible_projects(db, user)
    try:
        project_slug = await uc.delete(
            _ctx(user),
            db,
            ingest_id=ingest_id,
            project_root_for=_project_root,
            visible_projects=visible_projects,
        )
    except ServiceError as err:
        raise _to_http(err)
    await broadcast_ingest_changed(
        "deleted",
        workspace_id=user.workspace_id,
        ingest_id=ingest_id,
        project_slug=project_slug,
        status="deleted",
    )
    return Response(status_code=204)


@agent_only_route(router, "/pending/{ingest_id}", methods=["PATCH"], response_model=IngestPendingItem)
async def patch_ingest_pending(
    ingest_id: str,
    patch: IngestPendingPatch,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> IngestPendingItem:
    """E4 — Patch an ingest_pending row (project_slug change post-upload).

    Atomic file move via copy-then-rename (M-D11) with full path containment
    enforcement (H-D16). Optimistic concurrency via the `If-Match` header
    (compares against `updated_at`, millisecond resolution).

    State machine: project_slug change is allowed only when the row is in
    `awaiting_triage`, `parse_error`, or `rejected` (no in-flight saga).

    Transport boundary stays here: row load + visibility, the If-Match
    optimistic-lock 409, the allowed-state 422, slug-format 400, destination
    visibility, and ``_project_input_root`` resolution. The use_case
    (``patch_pending``) owns the sha-collision check, containment, atomic move,
    and the row + change-history writes.
    """
    # Load + visibility-check the row here so the optimistic-lock / state /
    # no-op short-circuits (which read row fields) keep their exact legacy
    # behaviour at the transport boundary.
    row = await _visible_row(db, ingest_id, user)

    # Optimistic lock — reject stale writes when caller pinned a snapshot.
    if if_match is not None and row["updated_at"] != if_match:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conflict_concurrent_modification",
                "current_updated_at": row["updated_at"],
            },
        )

    allowed_states = ("awaiting_triage", "parse_error", "rejected")
    if row["status"] not in allowed_states:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_state_for_change",
                "current_state": row["status"],
                "allowed_states": list(allowed_states),
            },
        )

    new_slug = patch.project_slug
    if not new_slug or new_slug == row["project_slug"]:
        # No-op: still return the current row so client can refresh.
        return _row_to_item(row)

    # H-D16 — slug regex whitelist (defense in depth before path resolution).
    if not _PROJECT_SLUG_RE.fullmatch(new_slug):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_slug_format",
                "message": (
                    f"Project slug '{new_slug}' must match [a-z0-9][a-z0-9_&-]+"
                ),
            },
        )

    # Verify visibility on the destination project too — operator must have
    # access to BOTH the source and destination projects.
    await check_project_access(new_slug, user, db)

    # Path containment + project existence check (reuses the upload helper to
    # keep the error contract identical between upload and patch).
    new_input_root = _project_input_root(new_slug)

    try:
        result = await uc.patch_pending(
            _ctx(user),
            db,
            row=row,
            new_slug=new_slug,
            new_input_root=new_input_root,
            projects_root=PROJECTS_ROOT,
            changed_by=user.username,
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ServiceError as err:
        raise _to_http(err)

    # Broadcast only on success (mirrors the legacy router — the SSE event
    # fires after the move + DB writes commit, never on a failed patch).
    await broadcast_ingest_changed(
        "changed",
        workspace_id=user.workspace_id,
        ingest_id=ingest_id,
        project_slug=new_slug,
        extra={"changes": ["project_slug"]},
    )
    return result


# --- P1.5.E7 MCP exposure surface (agent-native parity) ----------------------
# These endpoints back the new MCP tools defined in mcp-pir/index.mjs. The
# E5 Haiku/LLM hooks return 501 today and become functional after E5 deploy.


@agent_only_route(
    router,
    "/pending/{ingest_id}/classify-force",
    methods=["POST"],
    response_model=IngestDecisionResponse,
)
async def classify_force_ingest(
    ingest_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> IngestDecisionResponse:
    """Force re-trigger the parser/classifier for an ingest_pending row.

    Phase 1 = deterministic re-parse (same path used by `/pending/{id}/parse`).
    After P1.5.E5 lands, the underlying parser_router will invoke Haiku #1
    instead of the deterministic classifier — no MCP/UI change required.
    """
    visible_projects = await get_visible_projects(db, user)
    try:
        result = await uc.classify_force(
            _ctx(user), db, ingest_id=ingest_id, visible_projects=visible_projects
        )
    except ServiceError as err:
        raise _to_http(err)
    asyncio.create_task(parse_pending(ingest_id, user.workspace_id))
    return result


@agent_only_route(router, "/pending/{ingest_id}/reparse", methods=["POST"], response_model=IngestDecisionResponse)
async def reparse_single(
    ingest_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> IngestDecisionResponse:
    """Re-parse a single ingest_pending row.

    Useful for `parse_error` retry flow without changing project_slug. Same
    visibility/role checks as the rest of the triage surface.
    """
    visible_projects = await get_visible_projects(db, user)
    try:
        result = await uc.reparse_single(
            _ctx(user), db, ingest_id=ingest_id, visible_projects=visible_projects
        )
    except ServiceError as err:
        raise _to_http(err)
    asyncio.create_task(parse_pending(ingest_id, user.workspace_id))
    return result


class IngestReparseBatchBody(BaseModel):
    status: Literal["parse_error"] = "parse_error"


@agent_only_route(router, "/reparse-batch", methods=["POST"], response_model=IngestReparseBatchResponse)
async def reparse_batch(
    body: IngestReparseBatchBody,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> IngestReparseBatchResponse:
    """Re-parse all rows with the given status (default `parse_error`).

    Admin-only because it can fan out to N parser tasks. Mirrors
    `scripts/reparse_failed.py` but is API-driven so MCP/UI can trigger it.
    Visibility is enforced row-by-row downstream (parse_pending re-reads).
    """
    visible_projects = await get_visible_projects(db, user)
    try:
        result, ids = await uc.reparse_batch(
            _ctx(user),
            db,
            target_status=body.status,
            visible_projects=visible_projects,
        )
    except ServiceError as err:
        raise _to_http(err)
    for ingest_id in ids:
        asyncio.create_task(parse_pending(ingest_id, user.workspace_id))
    return result


@agent_only_route(router, "/pending/{ingest_id}/write-frontmatter", methods=["POST"])
async def write_frontmatter_placeholder(
    ingest_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
):
    """Placeholder for P1.5.E5 LLM-suggested frontmatter write (E5.T6 opt-in).

    Returns 501 until E5 lands. Documented now so MCP `write_haiku_frontmatter`
    has a stable endpoint to wrap and we surface a clear "not yet" rather than
    404 to agents.
    """
    raise HTTPException(status_code=501, detail="not_implemented_yet (Phase 1.5 E5)")


@router.get("/pending/{ingest_id}/preview.{ext}")
async def preview_ingest(
    ingest_id: str,
    ext: Literal["md", "pdf", "xlsx", "image"] = PathParam(...),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
):
    row = await _visible_row(db, ingest_id, user)
    path = _assert_project_path(row)
    suffix = path.suffix.lower()

    if ext == "md":
        if suffix not in {".md", ".markdown", ".txt"}:
            raise HTTPException(status_code=415, detail="Markdown preview unavailable")
        text = path.read_text(encoding="utf-8", errors="replace")
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    if ext == "pdf" and suffix == ".pdf":
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    if ext == "xlsx" and suffix in {".xlsx", ".xls"}:
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if suffix == ".xlsx"
            else "application/vnd.ms-excel"
        )
        return FileResponse(path, media_type=media_type, filename=path.name)

    if ext == "image" and suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }[suffix]
        return FileResponse(path, media_type=media_type, filename=path.name)

    raise HTTPException(
        status_code=415, detail="Preview type unavailable for this file"
    )
