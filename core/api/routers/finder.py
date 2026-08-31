# v1.12.0 - 2026-04-14 - Single-writer: make_directory uses get_write_db (batch 5/6)
from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import mimetypes
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from starlette.requests import Request

from core.api.templates.markdown_share import MAX_MARKDOWN_RENDER_SIZE

from core.api.config import settings
from core.api.db import get_db, get_write_db
from core.api.models import (
    FinderFileContent,
    FinderFileUpdate,
    FinderListItem,
    FinderListResponse,
    FinderTreeNode,
    UserInfo,
)
from core.api.routers._browser_mutation_denial import agent_only_route
from core.api.security import (
    get_agent_user,
    get_current_user,
    get_current_user_or_agent,
    is_local_single_user_mode,
    is_loopback_request,
)
from core.api.services import access_grants
from core.api.services import project_lifecycle
from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import AuthorizationError
from core.api.services.share_links import (
    create_shared_link_record,
    enforce_workspace_share_role,
    fetch_active_shared_path,
    is_workspace_share_path,
    mark_share_access,
    normalize_repo_input,
    public_repo_path,
    render_shared_target,
    resolve_shared_target,
    stored_repo_path,
    validate_repo_path,
)
from core.api.visibility import check_project_access, get_visible_projects

logger = logging.getLogger(__name__)

_LOCAL_HOST_DETAIL = (
    "This host-global filesystem operation is available only to the trusted "
    "local OSS loopback runtime."
)
_LEGACY_SHARE_DETAIL = (
    "Legacy host-global share routes are local-only. Use the governed share_file "
    "MCP tool for remote project-owned sharing."
)
_PROJECT_LIFECYCLE_DETAIL = (
    "Project roots and project.yaml are lifecycle control surfaces. "
    "Use the governed project lifecycle API."
)


def _require_local_host_request(request: Request) -> None:
    if is_local_single_user_mode() and is_loopback_request(request):
        return
    raise HTTPException(status_code=403, detail=_LOCAL_HOST_DETAIL)


def _require_local_legacy_share_request(request: Request) -> None:
    if is_local_single_user_mode() and is_loopback_request(request):
        return
    raise HTTPException(status_code=403, detail=_LEGACY_SHARE_DETAIL)


router = APIRouter(
    prefix="/api/v1/finder",
    tags=["finder"],
    dependencies=[Depends(_require_local_host_request)],
)
share_router = APIRouter(
    prefix="/api/v1",
    tags=["shared"],
    dependencies=[Depends(_require_local_legacy_share_request)],
)


# --- Security ---


def _authenticated_actor(user: UserInfo) -> CallerContext:
    actor = access_grants.actor_from_user_info(user)
    try:
        require_workspace_ctx(actor)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=exc.message) from exc
    return actor


def _finder_project_scopes(path: str) -> set[tuple[str, str]]:
    """Return both logical and resolved project scopes for one Finder path.

    Finder deliberately supports allow-listed symlinks.  A link below one
    project may therefore resolve into another project's tree.  Journaling
    only the user-facing path would let an active project mutate archived
    bytes through that link, so every mutation fences both identities.
    """
    scopes: set[tuple[str, str]] = set()
    pure = PurePosixPath((path or "").strip("/"))
    if len(pure.parts) >= 2 and pure.parts[0] == "projects":
        scopes.add((pure.parts[1], pure.as_posix()))

    projects_root = (Path(settings.finder_root).resolve() / "projects").resolve()
    resolved = _validate_path(path)
    try:
        resolved_relative = resolved.relative_to(projects_root)
    except ValueError:
        return scopes
    if resolved_relative.parts:
        resolved_ref = PurePosixPath(
            "projects",
            *resolved_relative.parts,
        ).as_posix()
        scopes.add((resolved_relative.parts[0], resolved_ref))
    return scopes


def _reject_project_lifecycle_path(*paths: str) -> None:
    """Keep generic Finder mutations away from project identity/lifecycle."""
    for path in paths:
        pure = PurePosixPath((path or "").strip("/"))
        if len(pure.parts) < 2 or pure.parts[0] != "projects":
            continue
        is_project_root = len(pure.parts) == 2
        is_project_metadata = (
            len(pure.parts) == 3 and pure.parts[2] == "project.yaml"
        )
        if is_project_root or is_project_metadata:
            raise HTTPException(status_code=403, detail=_PROJECT_LIFECYCLE_DETAIL)


@asynccontextmanager
async def _finder_project_mutation(
    db: aiosqlite.Connection,
    user: UserInfo,
    *,
    paths: list[str],
    writer_kind: str,
):
    """Fence every project touched by one local Finder filesystem mutation."""
    grouped: dict[str, list[str]] = {}
    for path in paths:
        for project_slug, resource_ref in _finder_project_scopes(path):
            grouped.setdefault(project_slug, []).append(resource_ref)
    if not grouped:
        yield
        return
    actor = _authenticated_actor(user)
    projects_root = (Path(settings.finder_root).resolve() / "projects").resolve()
    async with project_lifecycle.async_project_mutation_guard(
        projects_root=projects_root
    ):
        for project_slug, refs in sorted(grouped.items()):
            if not await project_lifecycle.project_write_fence_required(
                db,
                workspace_id=require_workspace_ctx(actor),
                project_slug=project_slug,
                projects_root=projects_root,
            ):
                continue
            await project_lifecycle.record_project_write(
                db,
                workspace_id=require_workspace_ctx(actor),
                project_slug=project_slug,
                writer_kind=writer_kind,
                actor=actor.user_id or actor.username,
                resource_ref=" -> ".join(refs),
                projects_root=projects_root,
            )
        await db.commit()
        yield


def _validate_path(raw_path: str) -> Path:
    """Resolve and jail path within finder_root. Returns absolute Path."""
    if "\x00" in raw_path:
        raise HTTPException(400, "Invalid path")

    root = Path(settings.finder_root).resolve()
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise HTTPException(403, "Access denied")

    current = root
    external_anchor: Path | None = None
    whitelist = [Path(w).resolve() for w in settings.finder_symlink_whitelist]

    # Hidden patterns check on the logical path requested by the client.
    for part in pure.parts:
        for pattern in settings.finder_hidden_patterns:
            if fnmatch.fnmatch(part, pattern):
                raise HTTPException(403, "Access denied")

    for part in pure.parts:
        candidate = current / part
        resolved = candidate.resolve()

        if external_anchor is None and not resolved.is_relative_to(root):
            allowed_anchor = next(
                (anchor for anchor in whitelist if resolved.is_relative_to(anchor)),
                None,
            )
            if allowed_anchor is None:
                raise HTTPException(403, "Access denied")
            external_anchor = allowed_anchor

        if external_anchor is not None and not resolved.is_relative_to(external_anchor):
            raise HTTPException(403, "Access denied")

        current = resolved

    target = current

    return target


def _rel_path(abs_path: Path) -> str:
    """Return path relative to finder_root. Does NOT resolve symlinks."""
    root = Path(settings.finder_root).resolve()
    resolved = abs_path.resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        pass

    for entry in root.iterdir():
        if not entry.is_symlink():
            continue
        try:
            link_dest = entry.resolve()
            rel = resolved.relative_to(link_dest)
        except (OSError, ValueError):
            continue
        return str(PurePosixPath(entry.name, *rel.parts))

    raise ValueError("Path is outside finder_root and allowed symlink targets")


def _is_hidden(name: str) -> bool:
    """Check if a filename matches hidden patterns."""
    for pattern in settings.finder_hidden_patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


async def _check_finder_visibility(
    path: str,
    user: UserInfo,
    db: aiosqlite.Connection,
) -> None:
    """Enforce project-level visibility on finder paths.

    DEFAULT-DENY for non-admin users:
    - super_admin/admin: unrestricted
    - operator/viewer: ONLY projects/{visible-slug}/ allowed
    - Everything else (pir/, repos/, root): blocked with 404

    Slug extracted from RESOLVED path (not raw input) to prevent symlink traversal.
    """
    actor = _authenticated_actor(user)
    # The whole Finder router is socket-guarded to the trusted OSS loopback
    # runtime.  Preserve that single-user runtime's host-filesystem semantics
    # even after migration 179 enables workspace isolation in the database.
    # A hosted admin never matches this principal and remains project-bound.
    if (
        actor.user_id == "local"
        and actor.username == "local"
        and actor.user_type == "human"
    ):
        return
    isolated = await access_grants.workspace_isolation_enabled(db)
    if not isolated and user.system_role in ("super_admin", "admin"):
        return

    resolved = _validate_path(path)
    root = Path(settings.finder_root).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise HTTPException(404, "Not found")
    parts = tuple(p for p in rel.parts if p)

    if not parts:
        raise HTTPException(404, "Not found")

    if parts[0] != "projects":
        raise HTTPException(404, "Not found")

    if len(parts) >= 2:
        slug = parts[1]
        await check_project_access(slug, user, db)


async def _check_finder_read(
    path: str,
    user: UserInfo,
    db: aiosqlite.Connection,
    *,
    direct_read: bool = False,
) -> None:
    await _check_finder_visibility(path, user, db)
    actor = _authenticated_actor(user)
    if not await access_grants.file_readable(
        db, actor, path, direct_read=direct_read
    ):
        raise HTTPException(404, "Not found")


async def _check_finder_write(
    path: str,
    user: UserInfo,
    db: aiosqlite.Connection,
) -> None:
    await _check_finder_visibility(path, user, db)
    actor = _authenticated_actor(user)
    if not await access_grants.file_writable(db, actor, path):
        raise HTTPException(404, "Not found")


# --- Endpoints ---


@router.get("/tree", response_model=list[FinderTreeNode])
async def get_tree(
    path: str = "",
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[FinderTreeNode]:
    """Directory tree for sidebar. Dirs only, max depth 3."""
    await _check_finder_visibility(path, current_user, db)
    target = _validate_path(path)
    if not target.is_dir():
        raise HTTPException(400, "Not a directory")

    def _scan_tree(dir_path: Path, depth: int = 0) -> list[dict]:
        if depth >= 3:
            return []
        nodes = []
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name.lower())
        except PermissionError:
            return []
        for entry in entries:
            if not entry.is_dir(follow_symlinks=True):
                continue
            if entry.name.startswith(".") or _is_hidden(entry.name):
                continue
            has_children = False
            try:
                has_children = any(
                    e.is_dir(follow_symlinks=True)
                    for e in os.scandir(entry.path)
                    if not e.name.startswith(".") and not _is_hidden(e.name)
                )
            except PermissionError:
                pass
            try:
                rel = _rel_path(Path(entry.path))
            except ValueError:
                continue
            nodes.append(
                {
                    "name": entry.name,
                    "path": rel,
                    "has_children": has_children,
                }
            )
        return nodes

    items = await asyncio.to_thread(_scan_tree, target)

    # Canonical workspace isolation applies to admins too.
    root_p = Path(settings.finder_root).resolve()
    rel_target = target.relative_to(root_p)
    parts = tuple(p for p in rel_target.parts if p)
    if not parts or parts == ("projects",):
        visible = await get_visible_projects(db, current_user)
        if visible is not None:
            items = [n for n in items if n.get("name") in visible]

    return [FinderTreeNode(**n) for n in items]


@router.get("/list", response_model=FinderListResponse)
async def list_directory(
    path: str = "",
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> FinderListResponse:
    """List files and directories with metadata."""
    await _check_finder_visibility(path, current_user, db)
    target = _validate_path(path)
    if not target.is_dir():
        raise HTTPException(400, "Not a directory")

    def _scan() -> list[dict]:
        items = []
        try:
            entries = list(os.scandir(target))
        except PermissionError:
            return []
        for entry in sorted(entries, key=lambda e: (not e.is_dir(), e.name.lower())):
            if _is_hidden(entry.name):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
                rel = _rel_path(Path(entry.path))
            except (PermissionError, OSError, ValueError):
                continue
            mime_type = None
            if not entry.is_dir():
                mime_type = mimetypes.guess_type(entry.name)[0]
            items.append(
                {
                    "name": entry.name,
                    "path": rel,
                    "is_dir": entry.is_dir(follow_symlinks=True),
                    "size": stat.st_size if not entry.is_dir() else 0,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "mime_type": mime_type,
                    "extension": Path(entry.name).suffix or None,
                }
            )
        return items

    items = await asyncio.to_thread(_scan)

    # Canonical workspace isolation applies to admins too.
    root_p = Path(settings.finder_root).resolve()
    try:
        rel_target = target.relative_to(root_p)
        parts = tuple(p for p in rel_target.parts if p)
        if not parts or parts == ("projects",):
            visible = await get_visible_projects(db, current_user)
            if visible is not None:
                items = [i for i in items if i.get("name") in visible]
    except ValueError:
        pass

    parent = str(Path(path).parent) if path else None
    if parent == ".":
        parent = ""
    return FinderListResponse(
        items=[FinderListItem(**i) for i in items],
        path=path,
        parent=parent,
    )


@router.get("/file", response_model=FinderFileContent)
async def read_file(
    path: str,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> FinderFileContent:
    """Read file content. Text returned as UTF-8, binary as base64."""
    await _check_finder_read(path, current_user, db, direct_read=True)
    target = _validate_path(path)
    if not target.is_file():
        raise HTTPException(404, "File not found")

    stat = await asyncio.to_thread(target.stat)
    if stat.st_size > settings.finder_max_view_bytes:
        raise HTTPException(413, "File too large")
    force_readonly = stat.st_size > settings.finder_max_edit_bytes

    # MIME-based binary detection (PDFs, images, archives have text-like headers)
    mime = mimetypes.guess_type(target.name)[0] or ""
    mime_binary = mime.startswith(
        ("image/", "audio/", "video/", "application/")
    ) and mime not in (
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-sh",
        "application/x-yaml",
    )

    # For large binary files, skip reading content entirely — client uses /download
    if mime_binary and stat.st_size > settings.finder_max_edit_bytes:
        return FinderFileContent(
            content="",
            filename=target.name,
            path=path,
            size=stat.st_size,
            mime_type=mime or None,
            encoding="base64",
            readonly=True,
        )

    raw = await asyncio.to_thread(target.read_bytes)

    # Binary detection: MIME hint OR null byte in first 512 bytes
    is_binary = mime_binary or b"\x00" in raw[:512]

    if is_binary:
        content = base64.b64encode(raw).decode("ascii")
        encoding = "base64"
    else:
        content = raw.decode("utf-8", errors="replace")
        encoding = "utf-8"

    return FinderFileContent(
        content=content,
        filename=target.name,
        path=path,
        size=stat.st_size,
        mime_type=mime or None,
        encoding=encoding,
        readonly=force_readonly or not os.access(target, os.W_OK),
    )


@agent_only_route(router, "/file", methods=["PUT"], response_model=FinderFileContent)
async def save_file(
    path: str,
    body: FinderFileUpdate,
    current_user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> FinderFileContent:
    """Save file content. Atomic write with fsync."""
    await _check_finder_write(path, current_user, db)
    actor = _authenticated_actor(current_user)
    target = _validate_path(path)
    _reject_project_lifecycle_path(path)
    if not target.exists():
        raise HTTPException(404, "File not found")
    if not os.access(target, os.W_OK):
        raise HTTPException(403, "File is read-only")

    content_bytes = body.content.encode("utf-8")
    if len(content_bytes) > settings.finder_max_edit_bytes:
        raise HTTPException(413, "Content too large")

    dir_path = target.parent

    def _write() -> None:
        fd, tmp = tempfile.mkstemp(dir=str(dir_path))
        try:
            os.write(fd, content_bytes)
            os.fsync(fd)
            os.close(fd)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async with _finder_project_mutation(
        db,
        current_user,
        paths=[path],
        writer_kind="finder_file",
    ):
        await asyncio.to_thread(_write)

        from core.api.services.confidential_files import capture_owner

        await capture_owner(db, actor, path)
        await db.commit()

    stat = await asyncio.to_thread(target.stat)
    return FinderFileContent(
        content=body.content,
        filename=target.name,
        path=path,
        size=stat.st_size,
        mime_type=mimetypes.guess_type(target.name)[0],
        encoding="utf-8",
        readonly=False,
    )


@agent_only_route(router, "/mkdir", methods=["POST"])
async def make_directory(
    data: dict,
    current_user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Create a new directory."""
    path = data.get("path", "")
    if not path:
        raise HTTPException(400, "Path required")

    await _check_finder_write(path, current_user, db)
    target = _validate_path(path)
    if target.exists():
        raise HTTPException(409, "Already exists")

    async with _finder_project_mutation(
        db,
        current_user,
        paths=[path],
        writer_kind="finder_mkdir",
    ):
        await asyncio.to_thread(target.mkdir, parents=False, exist_ok=False)
    return {"ok": True}


@agent_only_route(router, "/rename", methods=["POST"])
async def rename_item(
    data: dict,
    current_user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Rename a file or directory in place."""
    path = data.get("path", "")
    new_name = data.get("new_name", "")
    if not path or not new_name:
        raise HTTPException(400, "path and new_name required")

    # Validate new_name has no path separators
    if "/" in new_name or "\\" in new_name:
        raise HTTPException(400, "Invalid new name")

    await _check_finder_write(path, current_user, db)
    target = _validate_path(path)
    if not target.exists():
        raise HTTPException(404, "Not found")

    dest = target.parent / new_name
    # Validate dest is still inside jail
    _validate_path(_rel_path(dest))

    if dest.exists():
        raise HTTPException(409, f"'{new_name}' already exists")

    dest_rel = _rel_path(dest)
    _reject_project_lifecycle_path(path, dest_rel)
    async with _finder_project_mutation(
        db,
        current_user,
        paths=[path, dest_rel],
        writer_kind="finder_rename",
    ):
        await asyncio.to_thread(target.rename, dest)
        # RBAC F4: confidentiality travels with the file — a stale file_meta path
        # would silently declassify the renamed file on DB-authoritative checks.
        from core.api.services.confidential_files import migrate_file_meta_path

        await migrate_file_meta_path(
            db,
            old_path=path,
            new_path=dest_rel,
            workspace_id=require_workspace_ctx(_authenticated_actor(current_user)),
        )
        await db.commit()
    return {"ok": True}


@agent_only_route(router, "/upload", methods=["POST"], response_model=list[FinderListItem])
async def upload_files(
    path: str,
    files: list[UploadFile],
    current_user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> list[FinderListItem]:
    """Upload one or more files to a directory."""
    await _check_finder_write(path, current_user, db)
    target_dir = _validate_path(path)
    if not target_dir.is_dir():
        raise HTTPException(400, "Not a directory")

    prepared_files: list[tuple[UploadFile, str, Path]] = []
    for file in files:
        safe_name = PurePosixPath(file.filename or "unnamed").name
        if not safe_name:
            raise HTTPException(400, "Invalid filename")
        dest = target_dir / safe_name
        _reject_project_lifecycle_path(_rel_path(dest))
        prepared_files.append((file, safe_name, dest))

    results = []
    async with _finder_project_mutation(
        db,
        current_user,
        paths=[path],
        writer_kind="finder_upload",
    ):
        for file, safe_name, dest in prepared_files:
            if dest.exists():
                raise HTTPException(409, f"File already exists: {safe_name}")

            content = await file.read()
            if len(content) > settings.finder_max_upload_bytes:
                raise HTTPException(413, f"File too large: {safe_name}")

            await asyncio.to_thread(dest.write_bytes, content)

            from core.api.services.confidential_files import capture_owner

            await capture_owner(
                db,
                _authenticated_actor(current_user),
                _rel_path(dest),
            )

            stat = await asyncio.to_thread(dest.stat)
            results.append(
                FinderListItem(
                    name=safe_name,
                    path=_rel_path(dest),
                    is_dir=False,
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    mime_type=mimetypes.guess_type(safe_name)[0],
                    extension=Path(safe_name).suffix or None,
                )
            )
        await db.commit()

    return results


@router.get("/download")
async def download_file(
    path: str,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> StreamingResponse:
    """Download a file as binary stream."""
    await _check_finder_read(path, current_user, db, direct_read=True)
    target = _validate_path(path)
    if not target.is_file():
        raise HTTPException(404, "File not found")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"

    # Serve inline for browser-viewable types to avoid macOS Gatekeeper blocks
    inline_types = ("text/", "image/", "application/pdf")
    disposition = "inline" if mime_type.startswith(inline_types) else "attachment"

    def _iter_file():
        with open(target, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter_file(),
        media_type=mime_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{target.name}"',
            "Content-Length": str(target.stat().st_size),
        },
    )


@agent_only_route(router, "/delete", methods=["POST"])
async def delete_item(
    data: dict,
    current_user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Delete a file or directory."""
    path = data.get("path", "")
    if not path:
        raise HTTPException(400, "Path required")

    await _check_finder_write(path, current_user, db)
    target = _validate_path(path)
    if not target.exists():
        raise HTTPException(404, "Not found")

    # Safety: refuse to delete finder_root or its direct children
    root = Path(settings.finder_root).resolve()
    if target == root:
        raise HTTPException(403, "Cannot delete root")
    if target.parent == root and target.is_dir():
        raise HTTPException(403, "Cannot delete top-level directories")
    _reject_project_lifecycle_path(path)

    async with _finder_project_mutation(
        db,
        current_user,
        paths=[path],
        writer_kind="finder_delete",
    ):
        if target.is_dir():
            await asyncio.to_thread(shutil.rmtree, str(target))
        else:
            await asyncio.to_thread(target.unlink)

    return {"ok": True}


@agent_only_route(router, "/move", methods=["POST"])
async def move_item(
    data: dict,
    current_user: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Move a file or directory to a new location."""
    src_path = data.get("src", "")
    dest_path = data.get("dest", "")
    if not src_path or not dest_path:
        raise HTTPException(400, "src and dest required")

    await _check_finder_write(src_path, current_user, db)
    await _check_finder_write(dest_path, current_user, db)
    src = _validate_path(src_path)
    if not src.exists():
        raise HTTPException(404, "Source not found")

    dest_dir = _validate_path(dest_path)
    if not dest_dir.is_dir():
        raise HTTPException(400, "Destination must be a directory")

    final = dest_dir / src.name
    if final.exists():
        raise HTTPException(409, f"'{src.name}' already exists in destination")

    final_rel = _rel_path(final)
    _reject_project_lifecycle_path(src_path, final_rel)
    async with _finder_project_mutation(
        db,
        current_user,
        paths=[src_path, final_rel],
        writer_kind="finder_move",
    ):
        await asyncio.to_thread(shutil.move, str(src), str(final))
        # RBAC F4: confidentiality travels with the file (cross-project moves too).
        from core.api.services.confidential_files import migrate_file_meta_path

        await migrate_file_meta_path(
            db,
            old_path=src_path,
            new_path=final_rel,
            workspace_id=require_workspace_ctx(_authenticated_actor(current_user)),
        )
        await db.commit()
    return {"ok": True, "new_path": final_rel}


# --- Finder Pins ---


@router.get("/pins", response_model=list[dict])
async def list_pins(
    current_user=Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    cursor = await db.execute(
        "SELECT id, path, label, position FROM finder_pins WHERE user_id = ? ORDER BY position, id",
        (current_user.user_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r["id"],
            "path": r["path"],
            "label": r["label"],
            "position": r["position"],
        }
        for r in rows
    ]


@router.post("/pins", response_model=dict, status_code=201)
async def add_pin(
    body: dict,
    current_user=Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    path = body.get("path", "").strip()
    label = body.get("label") or None
    if not path:
        raise HTTPException(status_code=422, detail="path required")
    # Validate path is under allowed root
    _validate_path(path)
    cursor = await db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM finder_pins WHERE user_id = ?",
        (current_user.user_id,),
    )
    row = await cursor.fetchone()
    next_pos = row[0] if row else 0
    try:
        cursor = await db.execute(
            "INSERT INTO finder_pins (user_id, path, label, position) VALUES (?, ?, ?, ?)",
            (current_user.user_id, path, label, next_pos),
        )
        await db.commit()
        return {
            "id": cursor.lastrowid,
            "path": path,
            "label": label,
            "position": next_pos,
        }
    except Exception:
        raise HTTPException(status_code=409, detail="Path already pinned")


@router.delete("/pins/{pin_id}", status_code=204)
async def remove_pin(
    pin_id: int,
    current_user=Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    result = await db.execute(
        "DELETE FROM finder_pins WHERE id = ? AND user_id = ?",
        (pin_id, current_user.user_id),
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Pin not found")


# --- Shareable Links ---


async def _create_share_link_impl(
    data: dict,
    current_user: UserInfo,
    db: aiosqlite.Connection = Depends(get_db),
):
    path = data.get("path", "").strip()
    if not path:
        raise HTTPException(400, "path required")

    hours = data.get("hours", 24)
    if is_workspace_share_path(path):
        enforce_workspace_share_role(current_user)
        repo_rel_path = normalize_repo_input(path)
        target = validate_repo_path(repo_rel_path)
        if not target.is_file():
            raise HTTPException(404, "File not found")
        return await create_shared_link_record(
            stored_path=stored_repo_path(repo_rel_path),
            public_path=public_repo_path(repo_rel_path),
            current_user=current_user,
            db=db,
            hours=hours,
        )

    await _check_finder_read(path, current_user, db, direct_read=True)
    target = _validate_path(path)
    if not target.is_file():
        raise HTTPException(404, "File not found")

    return await create_shared_link_record(
        stored_path=path,
        public_path=path,
        current_user=current_user,
        db=db,
        hours=hours,
    )


@share_router.post("/share")
async def create_share_link(
    data: dict,
    current_user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Canonical share endpoint for both finder-root and workspace paths."""
    return await _create_share_link_impl(data, current_user, db)


@router.post("/share")
async def create_share_link_legacy(
    data: dict,
    current_user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Backward-compatible finder share endpoint."""
    return await _create_share_link_impl(data, current_user, db)


async def _list_shares_impl(
    current_user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    cursor = await db.execute(
        """SELECT id, token, path, created_at, expires_at, access_count
        FROM shared_links
        WHERE created_by = ? AND expires_at > datetime('now')
        ORDER BY created_at DESC""",
        (current_user.user_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r["id"],
            "token": r["token"],
            "path": r["path"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "access_count": r["access_count"],
        }
        for r in rows
    ]


@share_router.get("/shares")
async def list_shares(
    current_user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Canonical list of active shares created by current user."""
    return await _list_shares_impl(current_user, db)


@router.get("/shares")
async def list_shares_legacy(
    current_user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Backward-compatible finder shares endpoint."""
    return await _list_shares_impl(current_user, db)


async def _revoke_share_impl(
    token: str,
    current_user: UserInfo,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    result = await db.execute(
        "DELETE FROM shared_links WHERE token = ? AND created_by = ?",
        (token, current_user.user_id),
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "Share not found")


@share_router.delete("/share/{token}", status_code=204)
async def revoke_share(
    token: str,
    current_user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Canonical revoke endpoint."""
    await _revoke_share_impl(token, current_user, db)


@router.delete("/share/{token}", status_code=204)
async def revoke_share_legacy(
    token: str,
    current_user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Backward-compatible finder revoke endpoint."""
    await _revoke_share_impl(token, current_user, db)


# --- Public shared file access (no auth) ---
shared_router = APIRouter(prefix="/api/v1/shared", tags=["shared"])


async def _try_get_authenticated_user(
    request: Request, db: aiosqlite.Connection
) -> UserInfo | None:
    """Best-effort auth resolution for optional-auth endpoints.

    Returns UserInfo if the request carries a valid pir_session cookie or Bearer
    token; returns None on any auth failure (no cookie, expired, blacklisted, etc.).
    Used by GET /shared/<token>?format=json to compute the can_edit flag without
    breaking the public-no-auth contract on the same route.
    """
    try:
        return await get_current_user_or_agent(request=request, db=db)
    except HTTPException:
        return None
    except Exception:  # noqa: BLE001 - swallow JWT/decoding errors → unauth
        return None


@shared_router.get("/{token}")
async def access_shared_file(
    token: str,
    request: Request,
    format: str | None = Query(None, pattern="^json$"),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Access a shared file by token.

    Default: HTML SSR via render_shared_target (no auth required, public link).
    `?format=json`: returns JSON payload {filename, path, content, editable,
    can_edit, is_authenticated} for the React editor page. Auth is optional —
    can_edit is True only for authenticated users on workspace shares whose
    finder visibility check passes.
    """
    stored_path = await fetch_active_shared_path(token, db)
    target = resolve_shared_target(stored_path, _validate_path)
    await mark_share_access(token, db)

    if format == "json":
        user = await _try_get_authenticated_user(request, db)

        if not target.is_file():
            raise HTTPException(404, "File not found")

        try:
            rel = _rel_path(target)
        except ValueError:
            rel = ""

        # Editable: tutti i shares "regolari" (workspace + projects/...) sono
        # potenzialmente editabili. Il check effettivo (can_edit) e' fatto via
        # _check_finder_visibility sotto. Non vincoliamo piu' a workspace-only.
        can_edit = bool(user) and bool(rel)
        if can_edit and user is not None:
            try:
                await _check_finder_write(rel, user, db)
            except HTTPException:
                can_edit = False

        size = target.stat().st_size
        if size <= MAX_MARKDOWN_RENDER_SIZE:
            content = target.read_text(encoding="utf-8", errors="replace")
        else:
            content = ""

        return {
            "filename": target.name,
            "path": rel,
            "content": content,
            "editable": bool(rel),
            "can_edit": can_edit,
            "is_authenticated": bool(user),
        }

    return await render_shared_target(
        target,
        request,
        db,
        token=token,
        editable=True,
    )


@shared_router.put(
    "/{token}", dependencies=[Depends(_require_local_host_request)]
)
async def save_shared_file(
    token: str,
    body: FinderFileUpdate,
    request: Request,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Save a shared file.

    Requires authentication. Visibility is enforced via _check_finder_visibility
    (learning 5ffa60f0): non-admin users can only edit files inside
    `projects/{visible-slug}/`. Workspace shares are reachable solo a admin
    (default-deny mappa `workspace/*` fuori dai project). Public shares (link
    sharable) sono editabili solo se l'utente e' autenticato + ha visibility
    sul path — il token pubblico abilita lettura, NON scrittura senza auth.
    """
    stored_path = await fetch_active_shared_path(token, db)

    target = resolve_shared_target(stored_path, _validate_path)
    if not target.is_file():
        raise HTTPException(404, "Target is not a file")

    try:
        rel = _rel_path(target)
    except ValueError:
        raise HTTPException(404, "Target outside finder root")

    # Visibility check FIRST (anti-symlink): slug from resolved path.
    await _check_finder_write(rel, current_user, db)
    actor = _authenticated_actor(current_user)

    if not os.access(target, os.W_OK):
        raise HTTPException(403, "File is read-only")

    content_bytes = body.content.encode("utf-8")
    if len(content_bytes) > settings.finder_max_edit_bytes:
        raise HTTPException(413, "Content too large")

    dir_path = target.parent

    def _write() -> None:
        fd, tmp = tempfile.mkstemp(dir=str(dir_path))
        try:
            os.write(fd, content_bytes)
            os.fsync(fd)
            os.close(fd)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async with _finder_project_mutation(
        db,
        current_user,
        paths=[rel],
        writer_kind="finder_shared_file",
    ):
        await asyncio.to_thread(_write)

        from core.api.services.confidential_files import capture_owner

        await capture_owner(db, actor, rel)
        await db.commit()

    return {
        "path": rel,
        "size": len(content_bytes),
        "encoding": "utf-8",
        "filename": target.name,
    }
