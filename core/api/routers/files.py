# v1.1.0 - 2026-07-02 - RBAC hotfix: project visibility on GET/PUT, operator+ human-only PUT, audit write (task 5cc75139)
# v1.0.0 - 2026-02-25 - File read/write API for project files
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.routers._browser_mutation_denial import agent_only_route
from core.api.security import get_agent_user, get_current_user_or_agent
from core.api.services import access_grants
from core.api.services import project_lifecycle
from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import AuthorizationError
from core.api.visibility import check_project_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects", tags=["files"])

MAX_FILE_SIZE = 500_000  # 500KB
EDITABLE_EXTENSIONS = {".md"}
_FILE_PATH_RE = re.compile(r"^[\w\-./]+$")


def _authenticated_actor(user: UserInfo) -> CallerContext:
    actor = access_grants.actor_from_user_info(user)
    try:
        require_workspace_ctx(actor)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc
    return actor


def _get_project_path(slug: str) -> Path | None:
    """Resolve project slug to filesystem path (reuse project index)."""
    from core.api.routers.projects import _find_project_path
    return _find_project_path(slug)


def _validate_file_path(file_path: str) -> None:
    """Validate file path is safe (no traversal, no absolute)."""
    if not file_path or not _FILE_PATH_RE.match(file_path):
        raise HTTPException(status_code=400, detail="Invalid file path")
    if ".." in file_path:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if file_path.startswith("/"):
        raise HTTPException(status_code=400, detail="Absolute paths not allowed")


def _safe_resolve(project_path: Path, file_path: str) -> Path:
    """Resolve and validate containment within project directory."""
    target = (project_path / file_path).resolve()
    if not target.is_relative_to(project_path.resolve()):
        raise HTTPException(status_code=403, detail="Path outside project directory")
    if (project_path / file_path).is_symlink():
        raise HTTPException(status_code=403, detail="Symlinks not allowed")
    return target


class FileContent(BaseModel):
    content: str
    filename: str
    path: str
    size: int


class FileUpdate(BaseModel):
    content: str


async def _append_file_write_audit(
    db: aiosqlite.Connection,
    *,
    user: UserInfo,
    resource_id: str,
    stage: str,
    size: int,
    failure_type: str | None = None,
) -> None:
    from core.api.services.audit import log_audit

    if db.in_transaction:
        raise RuntimeError("files.write audit requires a clean transaction boundary")
    details = {"stage": stage, "size": size}
    if failure_type is not None:
        details["failure_type"] = failure_type
    action = "files.write" if stage == "confirmed" else f"files.write.{stage}"
    try:
        await db.execute("BEGIN IMMEDIATE")
        await log_audit(
            db,
            action=action,
            user=user.user_id or user.username,
            resource_type="project_file",
            resource_id=resource_id,
            details=details,
            workspace_id=require_workspace_ctx(_authenticated_actor(user)),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


@router.get(
    "/{slug}/files/{file_path:path}",
    response_model=FileContent,
)
async def read_file(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    file_path: str = PathParam(...),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> FileContent:
    """Read a file from project directory (project visibility enforced)."""
    _validate_file_path(file_path)
    actor = _authenticated_actor(user)

    # Visibility gate BEFORE project resolution: non-visible projects 404
    # without revealing whether they exist (same contract as finder).
    await check_project_access(slug, user, db)

    logical_path = f"{slug}/{file_path}"
    if not await access_grants.file_readable(
        db, actor, logical_path, direct_read=True
    ):
        raise HTTPException(status_code=404, detail="Not found")

    project_path = _get_project_path(slug)
    if not project_path:
        raise HTTPException(status_code=404, detail="Project not found")

    target = _safe_resolve(project_path, file_path)

    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        stat = target.stat()
        if stat.st_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="File too large (max 500KB)")
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {e}")

    return FileContent(
        content=content,
        filename=target.name,
        path=file_path,
        size=stat.st_size,
    )


@agent_only_route(
    router,
    "/{slug}/files/{file_path:path}",
    methods=["PUT"],
    response_model=FileContent,
)
async def write_file(
    body: FileUpdate,
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    file_path: str = PathParam(...),
    user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> FileContent:
    """Write a file in project directory (atomic write, .md only).

    Operator+ human sessions only (delegated agents included), project
    visibility enforced, write audited.
    """
    _validate_file_path(file_path)
    actor = _authenticated_actor(user)

    await check_project_access(slug, user, db)

    logical_path = f"{slug}/{file_path}"
    if not await access_grants.file_writable(db, actor, logical_path):
        raise HTTPException(status_code=404, detail="Not found")

    # Extension whitelist
    ext = Path(file_path).suffix.lower()
    if ext not in EDITABLE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Only {', '.join(EDITABLE_EXTENSIONS)} files can be edited",
        )

    project_path = _get_project_path(slug)
    if not project_path:
        raise HTTPException(status_code=404, detail="Project not found")

    target = _safe_resolve(project_path, file_path)

    # Size check
    content_bytes = body.content.encode("utf-8")
    if len(content_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Content too large (max 500KB)")

    resource_id = f"{slug}/{file_path}"
    async with project_lifecycle.guarded_project_file_write(
        actor,
        db,
        project_slug=slug,
        writer_kind="http_file",
        resource_ref=file_path,
        projects_root=project_path.parent,
    ):
        await _append_file_write_audit(
            db,
            user=user,
            resource_id=resource_id,
            stage="intent",
            size=len(content_bytes),
        )

        # Ensure parent directory exists only while archive is excluded.
        target.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: tempfile in same dir -> fsync -> os.replace
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent),
                prefix=".pir-write-",
                suffix=".tmp",
            )
            try:
                os.write(fd, content_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)

            os.replace(tmp_path, str(target))
            logger.info("File written: %s/%s by %s", slug, file_path, user.username)
        except OSError as e:
            try:
                if tmp_path is not None:
                    os.unlink(tmp_path)
            except OSError:
                pass
            await _append_file_write_audit(
                db,
                user=user,
                resource_id=resource_id,
                stage="failed",
                size=len(content_bytes),
                failure_type=type(e).__name__,
            )
            raise HTTPException(status_code=500, detail=f"Cannot write file: {e}")

        await _append_file_write_audit(
            db,
            user=user,
            resource_id=resource_id,
            stage="confirmed",
            size=len(content_bytes),
        )

        from core.api.services.confidential_files import capture_owner

        await capture_owner(db, actor, logical_path)
        await db.commit()

    return FileContent(
        content=body.content,
        filename=target.name,
        path=file_path,
        size=len(content_bytes),
    )
