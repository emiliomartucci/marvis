# v1.0.0 - 2026-02-25 - File read/write API for project files
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel

from core.api.models import UserInfo
from core.api.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects", tags=["files"])

MAX_FILE_SIZE = 500_000  # 500KB
EDITABLE_EXTENSIONS = {".md"}
_FILE_PATH_RE = re.compile(r"^[\w\-./]+$")


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


@router.get(
    "/{slug}/files/{file_path:path}",
    response_model=FileContent,
)
async def read_file(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    file_path: str = PathParam(...),
    user: UserInfo = Depends(get_current_user),
) -> FileContent:
    """Read a file from project directory."""
    _validate_file_path(file_path)

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


@router.put(
    "/{slug}/files/{file_path:path}",
    response_model=FileContent,
)
async def write_file(
    body: FileUpdate,
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    file_path: str = PathParam(...),
    user: UserInfo = Depends(get_current_user),
) -> FileContent:
    """Write a file in project directory (atomic write, .md only)."""
    _validate_file_path(file_path)

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

    # Ensure parent directory exists
    target.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: tempfile in same dir -> fsync -> os.replace
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
        # Cleanup temp file on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Cannot write file: {e}")

    return FileContent(
        content=body.content,
        filename=target.name,
        path=file_path,
        size=len(content_bytes),
    )
