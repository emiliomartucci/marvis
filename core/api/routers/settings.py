# v1.0.0 - 2026-02-25 - Settings router: project directories configuration
from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class ProjectDirsResponse(BaseModel):
    dirs: list[str]


class ProjectDirsUpdate(BaseModel):
    dirs: list[str]


def _expand_path(p: str) -> Path:
    """Expand ~ to home directory."""
    return Path(p).expanduser()


@router.get("/project-dirs")
async def get_project_dirs(
    user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> ProjectDirsResponse:
    """Get configured project directories."""
    cursor = await db.execute(
        "SELECT value FROM settings WHERE key = 'project_dirs'"
    )
    row = await cursor.fetchone()
    if row:
        dirs = json.loads(row["value"])
    else:
        dirs = ["~/workspace/projects"]
    return ProjectDirsResponse(dirs=dirs)


@router.put("/project-dirs")
async def update_project_dirs(
    body: ProjectDirsUpdate,
    user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> ProjectDirsResponse:
    """Update project directories. Validates paths exist."""
    # Validate: max 10 dirs, each must be absolute or ~-prefixed
    if len(body.dirs) > 10:
        raise HTTPException(400, "Maximum 10 directories allowed")

    validated: list[str] = []
    for d in body.dirs:
        d = d.strip()
        if not d:
            continue
        expanded = _expand_path(d)
        if not expanded.is_absolute():
            raise HTTPException(400, f"Path must be absolute or use ~: {d}")
        if not expanded.is_dir():
            raise HTTPException(400, f"Directory does not exist: {d}")
        validated.append(d)

    if not validated:
        raise HTTPException(400, "At least one directory required")

    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) "
        "VALUES ('project_dirs', ?, datetime('now'))",
        (json.dumps(validated),),
    )
    await db.commit()

    # Force rebuild of project index
    from core.api.routers.projects import _build_project_index, _set_project_dirs
    _set_project_dirs([_expand_path(d) for d in validated])
    _build_project_index()

    logger.info("Project dirs updated to: %s", validated)
    return ProjectDirsResponse(dirs=validated)
