# v1.0.0 - 2026-04-22 - Feed-style status updates for /projects/detail single-pager v2
"""Project status updates feed (PR #9).

Builds a chronological feed of mixed entries for a project:
  - Persisted rows from `project_status_updates` (kind = manual | auto_* | ai_*)
  - Derived on-the-fly entries from recent handoffs + git commits (non-persisted)

The goal is to give /projects/detail a "what happened lately" stream without
forcing every handoff/commit to be materialized in the DB. Rows are returned
newest-first, capped at `limit`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from core.api.models import StatusUpdateFeedItem

logger = logging.getLogger(__name__)

MAX_RECENT_HANDOFFS = 5
MAX_RECENT_COMMITS = 5
# Frontmatter/title regex for handoff summaries.
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_SUMMARY_KEY_RE = re.compile(r"^summary:\s*(.+?)(?:\n|$)", re.MULTILINE)
_TITLE_H1_RE = re.compile(r"^#\s+(.+?)$", re.MULTILINE)


def _parse_handoff_meta(path: Path) -> tuple[str | None, str]:
    """Extract (summary, session) from a handoff .md file.

    Returns a brief summary (from `summary:` frontmatter field, first H1, or
    first non-empty body line) and a session id if any. All I/O is bounded
    to ~8 KB to stay snappy.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")[:8192]
    except Exception as exc:  # noqa: BLE001 — don't blow up the feed on a broken file
        logger.debug("Failed to read handoff %s: %s", path, exc)
        return None, ""

    session = ""
    summary: str | None = None

    fm_match = _FRONTMATTER_RE.match(content)
    if fm_match:
        fm_body = fm_match.group(1)
        sum_match = _SUMMARY_KEY_RE.search(fm_body)
        if sum_match:
            summary = sum_match.group(1).strip().strip('"').strip("'")
        sess_match = re.search(r"^session:\s*(.+?)$", fm_body, re.MULTILINE)
        if sess_match:
            session = sess_match.group(1).strip().strip('"').strip("'")
        after_fm = content[fm_match.end():]
    else:
        after_fm = content

    if not summary:
        h1 = _TITLE_H1_RE.search(after_fm)
        if h1:
            summary = h1.group(1).strip()

    if not summary:
        # Fallback: first non-empty, non-heading line
        for line in after_fm.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("```"):
                summary = line[:280]
                break

    return summary, session


def _derive_from_handoffs(slug: str, metadata_path: Path | None) -> list[StatusUpdateFeedItem]:
    """Scan `{metadata_path}/memory/handoff-*.md` for the 5 most recent files."""
    if metadata_path is None:
        return []
    memory_dir = metadata_path / "memory"
    if not memory_dir.exists() or not memory_dir.is_dir():
        return []
    try:
        candidates = sorted(
            memory_dir.glob("handoff-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:MAX_RECENT_HANDOFFS]
    except OSError as exc:
        logger.debug("handoff scan failed for %s: %s", slug, exc)
        return []

    items: list[StatusUpdateFeedItem] = []
    for path in candidates:
        summary, session = _parse_handoff_meta(path)
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        content_md = summary or f"Handoff: {path.name}"
        if session:
            content_md = f"**Sessione {session}** · {content_md}"
        items.append(
            StatusUpdateFeedItem(
                id=f"handoff:{path.name}",
                kind="auto_handoff",
                author="handoff",
                author_display=path.name,
                content_md=content_md,
                ref_id=str(path.relative_to(metadata_path) if metadata_path in path.parents else path.name),
                created_at=mtime.isoformat(),
                derived=True,
            )
        )
    return items


async def _derive_from_commits(slug: str, repo_path: Path | None) -> list[StatusUpdateFeedItem]:
    """Run `git log -n 5 --format=...` against `repo_path`. Returns empty list when
    repo_path is None, not a git dir, or git is unavailable."""
    if repo_path is None or not (repo_path / ".git").exists():
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            f"-n{MAX_RECENT_COMMITS}",
            "--format=%H%x1f%s%x1f%an%x1f%aI",
            "--no-merges",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, _stderr_b = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return []
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("git log failed for %s at %s: %s", slug, repo_path, exc)
        return []

    out = stdout_b.decode("utf-8", errors="replace").strip()
    if not out:
        return []

    items: list[StatusUpdateFeedItem] = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, subject, author, iso_date = parts
        items.append(
            StatusUpdateFeedItem(
                id=f"commit:{sha[:7]}",
                kind="auto_commit",
                author="git",
                author_display=author or "unknown",
                content_md=subject.strip() or "(no commit message)",
                ref_id=sha,
                created_at=iso_date,
                derived=True,
            )
        )
    return items


def _row_to_feed_item(row: aiosqlite.Row) -> StatusUpdateFeedItem:
    kind = row["kind"] if "kind" in row.keys() and row["kind"] else "manual"
    author = row["created_by"]
    # Prefer stored content_md when present, fall back to composing from the
    # legacy structured fields (what_done / blockers / next_steps) so rows
    # created via /api/v1/status-updates still show up in the feed.
    raw_content = row["content_md"] if "content_md" in row.keys() else None
    if raw_content:
        content_md = raw_content
    else:
        chunks: list[str] = []
        if row["status"]:
            chunks.append(f"**Status:** {row['status']}")
        if row["what_done"]:
            chunks.append(f"**Done:** {row['what_done']}")
        if row["blockers"]:
            chunks.append(f"**Blockers:** {row['blockers']}")
        if row["next_steps"]:
            chunks.append(f"**Next:** {row['next_steps']}")
        content_md = "\n\n".join(chunks) if chunks else "(empty update)"
    author_display = row["author_display"] if "author_display" in row.keys() else None
    ref_id = row["ref_id"] if "ref_id" in row.keys() else None
    return StatusUpdateFeedItem(
        id=str(row["id"]),
        kind=kind,
        author=author,
        author_display=author_display,
        content_md=content_md,
        ref_id=ref_id,
        created_at=row["created_at"],
        derived=False,
    )


async def _require_unique_workspace_owner(
    db: aiosqlite.Connection,
    slug: str,
    workspace_id: str | None,
) -> None:
    """Recheck legacy slug ownership at the final DB read/write boundary."""
    if workspace_id is None:
        return
    try:
        owners = {
            str(row[0])
            for row in await (
                await db.execute(
                    "SELECT workspace_id FROM workspace_projects "
                    "WHERE project_slug = ?",
                    (slug,),
                )
            ).fetchall()
            if row[0]
        }
    except aiosqlite.Error as exc:
        raise LookupError("project not found") from exc
    if owners != {workspace_id}:
        raise LookupError("project not found")


async def list_feed(
    db: aiosqlite.Connection,
    slug: str,
    metadata_path: Path | None,
    repo_path: Path | None,
    limit: int = 20,
    workspace_id: str | None = None,
) -> tuple[list[StatusUpdateFeedItem], int]:
    """Combine stored updates + derived entries into a chronological feed."""
    await _require_unique_workspace_owner(db, slug, workspace_id)
    # Persisted rows (cap at limit just in case — typical project has <50)
    cursor = await db.execute(
        "SELECT id, project, status, what_done, blockers, next_steps, "
        "created_by, created_at, updated_at, kind, content_md, ref_id, author_display "
        "FROM project_status_updates WHERE project = ? ORDER BY created_at DESC LIMIT ?",
        (slug, limit),
    )
    db_rows = [_row_to_feed_item(r) async for r in cursor]

    # Derived entries, concurrent
    derived_handoffs = _derive_from_handoffs(slug, metadata_path)
    derived_commits = await _derive_from_commits(slug, repo_path)

    all_items = db_rows + derived_handoffs + derived_commits
    # Sort newest-first by ISO timestamp (lexicographic sort works for ISO 8601)
    all_items.sort(key=lambda item: item.created_at, reverse=True)

    total_derived = len(derived_handoffs) + len(derived_commits)
    return all_items[:limit], len(db_rows) + total_derived


async def create_manual_update(
    db: aiosqlite.Connection,
    slug: str,
    content_md: str,
    author: str,
    author_display: str | None,
    workspace_id: str | None = None,
) -> StatusUpdateFeedItem:
    """Insert a manual feed entry. Callers must have already enforced RBAC."""
    await _require_unique_workspace_owner(db, slug, workspace_id)
    now = datetime.now(timezone.utc).isoformat()
    # Legacy `status` column is NOT NULL, pick 'active' as a neutral default for
    # feed-mode entries (UI surfaces the kind/author, not the status badge).
    cursor = await db.execute(
        "INSERT INTO project_status_updates "
        "(project, status, kind, content_md, created_by, author_display, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (slug, "active", "manual", content_md, author, author_display, now),
    )
    await db.commit()
    new_id = cursor.lastrowid
    return StatusUpdateFeedItem(
        id=str(new_id),
        kind="manual",
        author=author,
        author_display=author_display,
        content_md=content_md,
        ref_id=None,
        created_at=now,
        derived=False,
    )
