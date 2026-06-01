# v1.0.0 - 2026-04-06 - Shared helpers for finder and workspace file shares
from __future__ import annotations

import fnmatch
import mimetypes
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import aiosqlite
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.requests import Request
from starlette.responses import HTMLResponse

from core.api.config import settings
from core.api.models import UserInfo
from core.api.templates.markdown_share import MAX_MARKDOWN_RENDER_SIZE, render_markdown_page

REPO_SHARE_PREFIX = "repo:"
WORKSPACE_PREFIX = "workspace"
REPO_SHARE_ROLES = ("operator", "admin", "super_admin")


def is_workspace_share_path(raw_path: str) -> bool:
    """True if a path (raw input or stored DB value) targets the workspace tree.

    Accepts both unprefixed (`workspace/foo.md`, raw user input) and prefixed
    (`repo:workspace/foo.md`, the form persisted in `shared_links.path`).
    """
    path = raw_path.strip().lstrip("/")
    if path.startswith(REPO_SHARE_PREFIX):
        path = path[len(REPO_SHARE_PREFIX):].lstrip("/")
    return path == WORKSPACE_PREFIX or path.startswith(f"{WORKSPACE_PREFIX}/")


def enforce_workspace_share_role(current_user: UserInfo) -> None:
    if current_user.system_role not in REPO_SHARE_ROLES:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


def normalize_repo_input(raw_path: str) -> str:
    path = raw_path.strip()
    if not path:
        raise HTTPException(400, "path required")
    if "\x00" in path:
        raise HTTPException(400, "Invalid path")

    rel_path = PurePosixPath(path.lstrip("/"))
    parts = tuple(part for part in rel_path.parts if part not in ("", "."))
    if parts and parts[0] == WORKSPACE_PREFIX:
        parts = parts[1:]

    normalized = str(PurePosixPath(*parts)) if parts else ""
    if not normalized:
        raise HTTPException(400, "path required")
    return normalized


def public_repo_path(repo_rel_path: str) -> str:
    return f"{WORKSPACE_PREFIX}/{repo_rel_path}"


def stored_repo_path(repo_rel_path: str) -> str:
    return f"{REPO_SHARE_PREFIX}{public_repo_path(repo_rel_path)}"


def parse_repo_stored_path(stored_path: str) -> tuple[str, str]:
    if not stored_path.startswith(REPO_SHARE_PREFIX):
        raise HTTPException(404, "Link not found or expired")

    public_path = stored_path[len(REPO_SHARE_PREFIX):]
    repo_rel_path = normalize_repo_input(public_path)
    return repo_rel_path, public_repo_path(repo_rel_path)


def validate_repo_path(repo_rel_path: str) -> Path:
    root = Path(settings.effective_repo_share_root).resolve()
    target = (root / repo_rel_path).resolve()

    if not target.is_relative_to(root):
        raise HTTPException(403, "Access denied")

    rel = target.relative_to(root)
    for part in rel.parts:
        for pattern in settings.finder_hidden_patterns:
            if fnmatch.fnmatch(part, pattern):
                raise HTTPException(403, "Access denied")

    return target


async def create_shared_link_record(
    *,
    stored_path: str,
    public_path: str,
    current_user: UserInfo,
    db: aiosqlite.Connection,
    hours: int | float,
    public_url_prefix: str = "/api/v1/shared",
) -> dict:
    if not isinstance(hours, (int, float)) or hours <= 0 or hours > 720:
        raise HTTPException(400, "hours must be between 1 and 720")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)

    await db.execute(
        "INSERT INTO shared_links (token, path, created_by, expires_at) VALUES (?, ?, ?, ?)",
        (token, stored_path, current_user.user_id, expires_at.isoformat()),
    )
    await db.commit()

    return {
        "token": token,
        "url": f"{public_url_prefix}/{token}",
        "expires_at": expires_at.isoformat(),
        "path": public_path,
    }


async def fetch_active_shared_path(token: str, db: aiosqlite.Connection) -> str:
    cursor = await db.execute(
        "SELECT path, expires_at FROM shared_links WHERE token = ?",
        (token,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Link not found or expired")

    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(410, "Link expired")

    return row["path"]


async def mark_share_access(token: str, db: aiosqlite.Connection) -> None:
    await db.execute(
        "UPDATE shared_links SET access_count = access_count + 1, last_accessed_at = datetime('now') WHERE token = ?",
        (token,),
    )
    await db.commit()


def resolve_shared_target(
    stored_path: str,
    finder_validate_path,
) -> Path:
    if stored_path.startswith(REPO_SHARE_PREFIX):
        try:
            repo_rel_path, _ = parse_repo_stored_path(stored_path)
            return validate_repo_path(repo_rel_path)
        except HTTPException as exc:
            if exc.status_code == 403:
                raise HTTPException(404, "File no longer accessible") from exc
            raise

    try:
        return finder_validate_path(stored_path)
    except HTTPException:
        raise HTTPException(404, "File no longer accessible")


async def render_shared_target(
    target: Path,
    request: Request,
    db: aiosqlite.Connection,
    token: str | None = None,
    editable: bool = False,
):
    if not target.is_file():
        raise HTTPException(404, "File not found")

    if target.suffix.lower() == ".md" and request.query_params.get("raw") != "1":
        file_size = target.stat().st_size
        if file_size <= MAX_MARKDOWN_RENDER_SIZE:
            content = target.read_text(encoding="utf-8")

            is_authenticated = False
            session_cookie = request.cookies.get("pir_session")
            if session_cookie:
                try:
                    from core.api.security import is_token_blacklisted, verify_session_jwt

                    payload = verify_session_jwt(session_cookie)
                    jti = payload.get("jti")
                    if jti:
                        is_authenticated = not await is_token_blacklisted(jti, db)
                    else:
                        is_authenticated = True
                except Exception:
                    pass

            # "Edit on console" link only for authenticated users on workspace shares.
            edit_token = token if (is_authenticated and editable) else None
            page, csp = render_markdown_page(
                content, target.name, is_authenticated, edit_token=edit_token
            )
            response = HTMLResponse(page)
            response.headers["Content-Security-Policy"] = csp
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    disposition = "inline" if mime_type.startswith(("text/", "image/", "application/pdf")) else "attachment"

    def _iter_file():
        with open(target, "rb") as handle:
            while chunk := handle.read(65536):
                yield chunk

    return StreamingResponse(
        _iter_file(),
        media_type=mime_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{target.name}"',
            "Content-Length": str(target.stat().st_size),
        },
    )
