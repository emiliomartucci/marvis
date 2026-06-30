# v4.0.0 - 2026-05-27 - S1 F1.5: thin adapter over use_cases.projects (filesystem/git/index helpers retained + re-exported)
# v3.7.0 - 2026-04-14 - git_push/git_pull use get_write_db (refactor batch 4/6)
"""HTTP adapter for the projects domain (S1 collapse-runtime, follows the learnings TEMPLATE).

This router is a thin transport adapter for the CRUD/query/visibility logic, which
lives in :mod:`core.api.use_cases.projects` (pure, fastapi-free). Each handler:

1. resolves identity into a :class:`CallerContext` (``from_user_info``);
2. for slug-scoped reads, resolves visibility (``get_visible_projects``) at the
   boundary and passes it in — the use_case ENFORCES it (DECISION 1: 404, does not
   reveal existence — parity with ``check_project_access``);
3. calls the use_case inside ``try/except ServiceError`` -> ``to_http``;
4. for ``get_project``, attaches ``deep`` KG context (rate-limit + log + lens) at
   the boundary (DECISION 2 — a per-surface concern).

CENTRAL-router note: the filesystem/discovery/git helpers + the project-index
globals (``PROJECT_DIRS``, ``_set_project_dirs``, ``_build_project_index``,
``_project_index``, ``_index_built_at``, ``_INDEX_TTL``, ``_find_project_entry``,
``_find_project_path``, ``_find_git_path``, ``_read_project_yaml``,
``_get_programs``, ``_parse_handoffs``, …) are INFRASTRUCTURE imported by ~12 other
modules directly from this path. They STAY here unchanged (mutable globals +
run-as-aware git command) and remain importable. The use_case reaches for them
function-locally so it stays fastapi-free.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import aiosqlite
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam

from core.api.config import settings
from core.api.db import get_db, get_write_db
from core.api.models import (
    DocEntry,
    HandoffEntry,
    ProgramInfo,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectDetail,
    ProjectUpdateRequest,
    StatusUpdateFeedCreateRequest,
    StatusUpdateFeedItem,
    StatusUpdateFeedResponse,
    UserInfo,
)
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import get_current_user, get_current_user_or_agent
from core.api.services import project_status_updates as status_feed_service  # noqa: F401  (re-export / used by use_case)
from core.api.visibility import check_project_access, get_visible_projects  # noqa: F401  (check_project_access kept as re-export seam)
from core.api.services.kg.audit import check_deep_rate_limit, log_kg_deep_access
from core.api.services.kg.lens import build_kg_context_for_project
from core.api.use_cases import projects as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

logger = logging.getLogger(__name__)

# Background task set (prevents GC of fire-and-forget embed coroutines) — the project
# twin of ``routers.learnings._bg_embed_learnings``.
_bg_embed_projects: set[asyncio.Task] = set()


def _schedule_embed_project(
    *,
    slug: str,
    description: str | None,
    workspace_id: str,
) -> None:
    """Fire-and-forget: embed a project in the background. No-ops if the embedder is
    unavailable. The project twin of ``learnings._schedule_embed_learning``.

    The create endpoint depends on the read pool (``Depends(get_db)``), and the embed
    body re-acquires the single-writer lock via ``write_db`` — so we ALWAYS defer with
    ``asyncio.create_task`` rather than awaiting inline (awaiting it here while holding
    the request connection would risk lock contention; learning f83f5209). The keying
    is derived to match ``use_cases.search._reindex_projects``: it reads ``project.yaml``
    where ``description`` is stored as ``description or ""``, so ``name`` (the doc_title)
    is ``description or slug`` and the embedded ``description`` is ``description or ""``.
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        return

    desc = description or ""
    name = desc or slug

    async def _embed() -> None:
        try:
            await embedding_service.embed_project_document(
                slug=slug,
                name=name,
                description=desc,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.debug(
                "Auto-embed project %s failed (non-critical)",
                slug,
                exc_info=True,
            )

    t = asyncio.create_task(_embed())
    _bg_embed_projects.add(t)
    t.add_done_callback(_bg_embed_projects.discard)


router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

def _default_project_dirs() -> list[Path]:
    """Initial project roots for non-FastAPI runtimes.

    FastAPI startup and the settings router can still override PROJECT_DIRS, but
    the hosted MCP process does not run that lifespan. It must honor the tenant
    env from process start.
    """
    env_root = os.environ.get("MARVIS_PROJECTS_ROOT")
    if env_root and env_root.strip():
        return [Path(env_root).expanduser()]
    return [Path.home() / "workspace" / "projects"]


# Project base directories (default, overridden by DB settings / startup)
PROJECT_DIRS: list[Path] = _default_project_dirs()


def _set_project_dirs(dirs: list[Path]) -> None:
    """Update project directories (called from settings router or startup)."""
    global PROJECT_DIRS
    PROJECT_DIRS = dirs

MAX_FILE_SIZE = 500_000  # 500KB per file

# --- ProjectIndexEntry: rich index with type awareness ---

ProjectType = Literal["work", "code", "system"]


@dataclass
class ProjectIndexEntry:
    slug: str
    metadata_path: Path
    repo_path: Path | None
    project_type: ProjectType


from core.api.config import ALLOWED_REPO_PARENTS  # noqa: E402  # centralized in config.py
from core.api.services.runas import GIT_CMD as _RUNAS_GIT_CMD  # noqa: E402

_project_index: dict[str, ProjectIndexEntry] = {}
_index_built_at: float = 0
_INDEX_TTL = 300  # 5 minutes

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$")


def _build_project_index() -> dict[str, ProjectIndexEntry]:
    """Build slug -> ProjectIndexEntry mapping from .task files + project.yaml."""
    global _project_index, _index_built_at
    index: dict[str, ProjectIndexEntry] = {}
    for base in PROJECT_DIRS:
        if not base.exists():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.is_symlink():
                continue
            task_file = d / ".task"
            if task_file.exists():
                try:
                    slug = task_file.read_text().strip()[:64]
                except OSError:
                    continue
                if not slug or not _SLUG_RE.match(slug):
                    continue
            else:
                if _SLUG_RE.match(d.name):
                    slug = d.name
                else:
                    continue

            # Read project.yaml for type and repo_path
            yaml_data = _read_project_yaml(d)
            project_type: ProjectType = "work"
            repo_path: Path | None = None

            if yaml_data:
                raw_type = yaml_data.get("type")
                if raw_type in ("work", "code", "system"):
                    project_type = raw_type
                else:
                    project_type = "code" if (d / ".git").exists() else "work"

                repo_path_str = yaml_data.get("repo_path")
                if repo_path_str:
                    rp = Path(repo_path_str).resolve()
                    if rp.is_dir() and any(rp.is_relative_to(p) for p in ALLOWED_REPO_PARENTS):
                        repo_path = rp
                    elif not rp.exists():
                        # An imported/migrated project.yaml carries a repo_path from
                        # another host (e.g. a hosted tenant restored from export)
                        # that does not exist here. Skip code-KG for it (repo_path
                        # stays None) instead of warning loudly per project.
                        logger.debug(
                            "repo_path absent on this host for %s: %s (skipping code-KG)",
                            slug,
                            repo_path_str,
                        )
                    else:
                        logger.warning("Invalid repo_path for %s: %s", slug, repo_path_str)
                elif (d / ".git").exists():
                    repo_path = d.resolve()
            else:
                project_type = "code" if (d / ".git").exists() else "work"
                if (d / ".git").exists():
                    repo_path = d.resolve()

            if slug in index:
                logger.warning("Duplicate slug '%s': %s vs %s", slug, index[slug].metadata_path, d)

            index[slug] = ProjectIndexEntry(
                slug=slug,
                metadata_path=d,
                repo_path=repo_path,
                project_type=project_type,
            )
    _project_index = index
    _index_built_at = time.monotonic()
    return index


def _find_project_path(slug: str) -> Path | None:
    """O(1) lookup returning metadata_path with containment check."""
    global _project_index, _index_built_at
    if not _SLUG_RE.match(slug):
        return None
    if time.monotonic() - _index_built_at > _INDEX_TTL:
        _build_project_index()
    entry = _project_index.get(slug)
    if entry is None:
        return None
    resolved = entry.metadata_path.resolve()
    for base in PROJECT_DIRS:
        if resolved.is_relative_to(base.resolve()):
            return resolved
    return None


def _find_project_entry(slug: str) -> ProjectIndexEntry | None:
    """O(1) lookup returning full ProjectIndexEntry."""
    if not _SLUG_RE.match(slug):
        return None
    if time.monotonic() - _index_built_at > _INDEX_TTL:
        _build_project_index()
    return _project_index.get(slug)


def _find_git_path(slug: str) -> Path | None:
    """Resolve slug to git repo path for git operations."""
    entry = _find_project_entry(slug)
    if not entry or not entry.repo_path:
        return None
    repo = entry.repo_path.resolve()
    if not any(repo.is_relative_to(p) for p in ALLOWED_REPO_PARENTS):
        logger.warning("repo_path containment violation for %s: %s", slug, repo)
        return None
    if not (repo / ".git").exists():
        return None
    return repo


# --- programs.yaml cache ---

_programs_cache: dict | None = None
_programs_mtime: float = 0


def _get_programs() -> dict:
    """Load programs.yaml with mtime-based cache."""
    global _programs_cache, _programs_mtime
    yaml_path = Path.home() / "workspace" / "programs.yaml"
    if not yaml_path.exists():
        return {}
    try:
        current_mtime = yaml_path.stat().st_mtime
        if _programs_cache is not None and current_mtime == _programs_mtime:
            return _programs_cache
        _programs_cache = yaml.safe_load(yaml_path.read_text()) or {}
        _programs_mtime = current_mtime
        return _programs_cache
    except Exception:
        logger.exception("Failed to load programs.yaml")
        return _programs_cache or {}


# --- project.yaml mtime cache ---

_project_yaml_cache: dict[str, dict | None] = {}
_project_yaml_mtime: dict[str, float] = {}


def _read_project_yaml(project_path: Path) -> dict | None:
    """Read project.yaml with mtime-based cache. Returns dict or None."""
    yaml_path = project_path / "project.yaml"
    if not yaml_path.exists():
        return None
    slug = project_path.name
    try:
        current_mtime = yaml_path.stat().st_mtime
        if slug in _project_yaml_cache and _project_yaml_mtime.get(slug) == current_mtime:
            return _project_yaml_cache[slug]
        content = _safe_read_file(project_path, "project.yaml")
        if not content:
            return None
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return None
        _project_yaml_cache[slug] = data
        _project_yaml_mtime[slug] = current_mtime
        return data
    except Exception:
        logger.warning("Failed to parse project.yaml for %s", slug, exc_info=True)
        return _project_yaml_cache.get(slug)


def _get_project_metadata(project_path: Path) -> dict:
    """Get project metadata from project.yaml, fallback to context.md Config."""
    yaml_data = _read_project_yaml(project_path)
    if yaml_data:
        return yaml_data
    ctx = _safe_read_file(project_path, "context.md")
    if ctx:
        return _parse_context_config(ctx)
    return {}


# --- Safe file reading ---


def _safe_read_file(project_path: Path, relative_path: str) -> str | None:
    """Read file within project dir with containment + size limit."""
    target = (project_path / relative_path).resolve()
    if not target.is_relative_to(project_path.resolve()):
        return None
    if (project_path / relative_path).is_symlink():
        return None
    if not target.is_file():
        return None
    try:
        if target.stat().st_size > MAX_FILE_SIZE:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# --- Task counts batch query ---


async def _get_all_task_counts(db: aiosqlite.Connection) -> dict[str, dict[str, int]]:
    """Single GROUP BY query for all projects."""
    cursor = await db.execute(
        "SELECT project, status, COUNT(*) as cnt "
        "FROM tasks WHERE deleted_at IS NULL "
        "GROUP BY project, status"
    )
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    async for row in cursor:
        result[row["project"]][row["status"]] = row["cnt"]
    return dict(result)


# --- Latest status update per project ---


async def _get_latest_status_updates(db: aiosqlite.Connection) -> dict[str, tuple[str, str]]:
    """Latest status update (status, date) per project."""
    cursor = await db.execute(
        "SELECT project, status, created_at FROM project_status_updates "
        "WHERE id IN (SELECT MAX(id) FROM project_status_updates GROUP BY project)"
    )
    result: dict[str, tuple[str, str]] = {}
    async for row in cursor:
        result[row["project"]] = (row["status"], row["created_at"])
    return dict(result)


# --- Helpers ---


def _parse_context_config(content: str) -> dict:
    """Extract key-value pairs from ## Config section of context.md."""
    config: dict[str, str] = {}
    in_config = False
    for line in content.splitlines():
        if line.strip().startswith("## Config"):
            in_config = True
            continue
        if in_config and line.strip().startswith("## "):
            break
        if in_config and line.strip().startswith("- "):
            parts = line.strip()[2:].split(":", 1)
            if len(parts) == 2:
                key = parts[0].strip().strip("*")
                config[key.lower()] = parts[1].strip()
    return config


def _handoff_date_from_content(content: str, filename: str) -> str:
    """Return the handoff date from frontmatter, falling back to the filename."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            try:
                fm = yaml.safe_load(content[3:end])
                if isinstance(fm, dict) and "date" in fm:
                    return str(fm["date"])
            except Exception:
                pass

    date_match = re.search(r"handoff-(\d{4}-\d{2}-\d{2})", filename)
    return date_match.group(1) if date_match else ""


def _handoff_sort_key(path: Path, date: str) -> tuple[int, str, float, str]:
    """Prefer dated handoffs; for same-date files, newest mtime wins."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (1 if date else 0, date, mtime, path.name)


def _parse_handoffs(project_path: Path) -> list[HandoffEntry]:
    """Parse memory/handoff-*.md files. Reads YAML frontmatter if present, fallback to filename."""
    memory_dir = project_path / "memory"
    if not memory_dir.is_dir():
        return []
    entries: list[HandoffEntry] = []
    candidates: list[tuple[Path, str, str]] = []
    for f in memory_dir.glob("handoff-*.md"):
        if f.is_symlink():
            continue
        content = _safe_read_file(project_path, f"memory/{f.name}")
        if not content:
            continue
        candidates.append((f, content, _handoff_date_from_content(content, f.name)))

    for f, content, date in sorted(
        candidates,
        key=lambda item: _handoff_sort_key(item[0], item[2]),
        reverse=True,
    ):

        session = None
        branch = None
        tags: list[str] = []

        # Try YAML frontmatter first
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                try:
                    fm = yaml.safe_load(content[3:end])
                    if isinstance(fm, dict):
                        date = str(fm["date"]) if "date" in fm else ""
                        raw_session = fm.get("session")
                        if raw_session not in (None, ""):
                            session = str(raw_session)
                        branch = fm.get("branch")
                        tags = fm.get("tags", [])
                        if isinstance(tags, str):
                            tags = [tags]
                except Exception:
                    pass

        # Extract summary from ## Summary section (always, regardless of frontmatter)
        summary = ""
        in_summary = False
        for line in content.splitlines():
            if line.strip().startswith("## Summary"):
                in_summary = True
                continue
            if in_summary and line.strip().startswith("## "):
                break
            if in_summary and line.strip():
                summary += line.strip() + " "
                if len(summary) > 200:
                    break

        entries.append(HandoffEntry(
            filename=f"memory/{f.name}",
            date=date,
            summary=summary.strip()[:200],
            session=session,
            branch=branch,
            tags=tags,
        ))
    return entries


def _parse_docs(project_path: Path, subdir: str) -> list[DocEntry]:
    """Parse docs/plans/ or docs/solutions/ files."""
    docs_dir = project_path / "docs" / subdir
    if not docs_dir.is_dir():
        return []
    entries: list[DocEntry] = []
    for f in sorted(docs_dir.glob("*.md"), reverse=True):
        if f.is_symlink():
            continue
        content = _safe_read_file(project_path, f"docs/{subdir}/{f.name}")
        title = None
        date = None
        category = None
        if content:
            # Parse YAML frontmatter
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    try:
                        fm = yaml.safe_load(content[3:end])
                        if isinstance(fm, dict):
                            title = fm.get("title")
                            date = str(fm["date"]) if "date" in fm else None
                            category = fm.get("category") or fm.get("type")
                    except Exception:
                        pass
        if not date:
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            date = date_match.group(1) if date_match else None
        if not title:
            title = f.stem
        entries.append(DocEntry(filename=f"docs/{subdir}/{f.name}", date=date, title=title, category=category))
    return entries


# --- Git operations ---

# Run-as-aware git (see services/runas.py): `sudo -u <user> git` when a run-as
# user is configured, else plain `git`. Tuple so it unpacks into
# create_subprocess_exec(*_GIT_CMD, "log", ...).
_GIT_CMD: tuple[str, ...] = tuple(_RUNAS_GIT_CMD)

_GIT_REMOTE_RE = re.compile(r"^(https://github\.com/|git@github\.com:)")


async def _validate_git_remote(project_path: Path) -> None:
    """SSRF prevention: only allow operations to known remotes."""
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "remote", "get-url", "origin",
        cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    remote_url = stdout.decode().strip()
    if not _GIT_REMOTE_RE.match(remote_url):
        raise HTTPException(403, "Git remote not allowed (only github.com)")


async def _git_log(project_path: Path, limit: int = 20) -> list[dict]:
    """Get git log as list of commit dicts."""
    limit = max(1, min(limit, 100))
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "log", f"--max-count={limit}",
        "--format=%H%x09%h%x09%s%x09%an%x09%aI",
        cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        return []
    commits = []
    for line in stdout.decode().strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            commits.append({
                "hash": parts[0],
                "hash_short": parts[1],
                "message": parts[2],
                "author": parts[3],
                "date": parts[4],
            })
    return commits


async def _git_diff(project_path: Path) -> str:
    """Get git diff (staged + unstaged), truncated to 100KB."""
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "diff", "--stat",
        cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stat_out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)

    proc2 = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "diff",
        cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    diff_out, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)

    stat_str = stat_out.decode(errors="replace")
    diff_str = diff_out.decode(errors="replace")
    # Truncate to 100KB total
    combined = f"{stat_str}\n---\n{diff_str}"
    return combined[:100_000]


async def _git_branches(project_path: Path) -> list[dict]:
    """Get list of branches with current indicator."""
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "branch", "--format=%(refname:short)%09%(HEAD)",
        cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        return []
    branches = []
    for line in stdout.decode().strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            branches.append({
                "name": parts[0],
                "is_current": parts[1].strip() == "*",
            })
    return branches


async def _git_graph_log(
    project_path: Path, limit: int = 50, skip: int = 0, all_branches: bool = True,
) -> list[dict]:
    """Get git log with parent hashes and decorations for graph rendering."""
    limit = max(1, min(limit, 200))
    cmd = [
        *_GIT_CMD, "log", "--topo-order",
        f"--max-count={limit}", f"--skip={skip}",
        "--format=%H%x00%P%x00%D%x00%s%x00%an%x00%aI",
    ]
    if all_branches:
        cmd.insert(len(_GIT_CMD) + 1, "--all")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        return []
    commits = []
    for line in stdout.decode().strip().splitlines():
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) >= 6:
            commits.append({
                "hash": parts[0],
                "hash_short": parts[0][:7],
                "parents": parts[1].split() if parts[1] else [],
                "refs": [r.strip() for r in parts[2].split(", ")] if parts[2] else [],
                "message": parts[3],
                "author": parts[4],
                "date": parts[5],
            })
    return commits


async def _git_refs(project_path: Path) -> list[dict]:
    """Get all refs (branches + tags) with their target commits."""
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "for-each-ref",
        "--format=%(refname:short)%x00%(objectname:short)%x00%(objecttype)",
        "refs/heads", "refs/tags",
        cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        return []
    refs = []
    for line in stdout.decode().strip().splitlines():
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) >= 3:
            refs.append({"name": parts[0], "hash_short": parts[1], "type": parts[2]})
    return refs


async def _git_commit_detail(project_path: Path, commit_hash: str) -> dict | None:
    """Get detailed info for a single commit."""
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "show", "--stat", "--format=%H%x00%B%x00%an%x00%ae%x00%aI",
        commit_hash,
        cwd=str(project_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        return None
    output = stdout.decode(errors="replace")
    # Format output: first line is header fields separated by \x00, then a blank line,
    # then the body (which may contain \x00 from the %B placeholder),
    # then --stat output.
    # We use a simpler approach: split on first \x00 occurrences for header fields.
    first_null = output.find("\x00")
    if first_null < 0:
        return None
    full_hash = output[:first_null]
    rest = output[first_null + 1:]
    # Find remaining fields: body\x00author\x00email\x00date
    # Body can be multiline so we find from the end
    parts = rest.split("\x00")
    if len(parts) < 4:
        return None
    date_str = parts[-1].split("\n")[0].strip()
    email = parts[-2].strip()
    author = parts[-3].strip()
    body = "\x00".join(parts[:-3]).strip()
    # Extract stat lines (after body, typically after an empty line + stat output)
    stat_lines = []
    in_stats = False
    for line in output.splitlines():
        if line.strip().startswith("|") or (line.strip() and "changed" in line and ("insertion" in line or "deletion" in line)):
            in_stats = True
        if in_stats and line.strip():
            stat_lines.append(line.strip())
    return {
        "hash": full_hash,
        "body": body,
        "author": author,
        "email": email,
        "date": date_str,
        "stats": stat_lines,
    }


# --- Endpoints ---


async def _resolve_git_repo(
    slug: str, user: UserInfo, db: aiosqlite.Connection
) -> Path:
    """Adapter helper for the ``git/*`` endpoints: resolve slug -> repo Path.

    Resolves visibility at the boundary (DECISION 1) and delegates to
    ``uc.resolve_git_repo`` which enforces it (404), then maps the domain error to
    HTTP: missing project -> 404, present-but-non-git -> 400 (via
    ``NoGitRepoError.http_status``). Same 404-vs-400 split as the pre-refactor
    router, just routed through ``to_http``.
    """
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.resolve_git_repo(ctx, db, slug=slug, visible_projects=visible_projects)
    except ServiceError as e:
        raise to_http(e)


@router.get("")
async def list_programs(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[ProgramInfo]:
    """List all programs with their projects and task counts."""
    # DECISION 1 (visibility template): resolve at the boundary (needs
    # UserInfo.teams/user_id, not carried by CallerContext) and pass it in; the
    # use_case applies it as an inline filter (aggregate listing, no raise).
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.list_programs(ctx, db, visible_projects=visible_projects)
    except ServiceError as e:
        raise to_http(e)


def _create_project_on_disk(
    *,
    slug: str,
    display_name: str,
    program: str | None,
    scope: str | None,
    description: str | None,
    lifecycle: str,
    language: str | None,
    type: str,
) -> str:
    """Create the project directory tree on disk and return its path.

    Filesystem side-effect kept in the router (out of the pure use_case): builds
    ``project.yaml`` + ``context.md`` + ``.task`` + subdirs under
    ``/data/projects/{slug}/``, invalidates the project index, and returns the
    metadata path. Raises a :class:`ServiceError` (``http_status = 500``) on
    ``OSError`` with cleanup — parity with the original ``HTTPException(500)``.
    Callers (the use_case) must have already validated non-existence + RBAC.
    """
    # Resolved root: /data/projects on the managed deploy, the configured
    # projects_root on the local tier (gh #17).
    from core.api.use_cases.projects import data_project_dir

    project_dir = data_project_dir() / slug
    today = date.today().isoformat()

    yaml_data = {
        "project": slug,
        "program": program,
        "scope": scope or "work",
        "description": description or "",
        "lifecycle": lifecycle,
        "phase": "",
        "language": language or "none",
        "stack": [],
        "type": type,
        "repo_path": None,
        "last_session": 0,
        "last_work": None,
    }

    context_md = (
        f"# {display_name}\n"
        f"\n"
        f"> Metadati strutturati in `project.yaml`\n"
        f"\n"
        f"## Obiettivo\n"
        f"\n"
        f"{description or ''}\n"
        f"\n"
        f"## Status\n"
        f"- Ultimo lavoro: {today}\n"
        f"\n"
        f"## Task Attivi\n"
        f"> Tracciati su Task API: `GET /api/v1/tasks?project={slug}&status=pending`\n"
    )

    try:
        project_dir.mkdir(parents=False, exist_ok=False)
        for subdir in ["memory", "docs/brainstorms", "docs/plans", "docs/solutions",
                        "input", "output", "scripts"]:
            (project_dir / subdir).mkdir(parents=True, exist_ok=True)
        for keep_dir in ["input", "output", "scripts"]:
            (project_dir / keep_dir / ".gitkeep").touch()
        (project_dir / ".task").write_text(slug + "\n")
        (project_dir / "project.yaml").write_text(
            yaml.dump(yaml_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )
        (project_dir / "context.md").write_text(context_md)
    except OSError as exc:
        if project_dir.exists():
            shutil.rmtree(project_dir, ignore_errors=True)
        raise ServiceError(
            code="project_create_failed",
            message=f"Failed to create project directory: {exc}",
        ) from exc

    # Invalidate project index so the new project is immediately visible
    global _index_built_at
    _index_built_at = 0

    return str(project_dir)


@router.post("", status_code=201)
async def create_project(
    body: ProjectCreateRequest,
    user: UserInfo = Depends(require_role("operator")),
    db: aiosqlite.Connection = Depends(get_db),
) -> ProjectCreateResponse:
    """Create a new work project on the server.

    Creates the directory structure, project.yaml, context.md, and .task file
    under /data/projects/{slug}/.
    """
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        result = await uc.create_project(
            ctx,
            db,
            slug=body.slug,
            name=body.name,
            program=body.program,
            scope=body.scope,
            description=body.description,
            lifecycle=body.lifecycle,
            language=body.language,
            type=body.type,
        )
        # Embed-on-write so the just-created project is immediately searchable by
        # meaning (keyword-only until a manual reindex otherwise). Fire-and-forget:
        # a failed/slow embed never fails or blocks the create.
        _schedule_embed_project(
            slug=result.slug,
            description=result.description,
            workspace_id=ctx.workspace_id,
        )
        return result
    except ServiceError as e:
        raise to_http(e)


@router.get("/{slug}")
async def get_project(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    deep_param: bool | None = Query(None, alias="deep"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> ProjectDetail:
    """Get project detail with context.md, config, handoffs, plans, solutions."""
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        detail = await uc.get_project(ctx, db, slug=slug, visible_projects=visible_projects)
    except ServiceError as e:
        raise to_http(e)

    # DECISION 2: deep KG enrichment is a per-surface (transport) concern — the
    # rate-limit + access-log + lens stay in the adapter, attached after the
    # core ProjectDetail is built. Behavior identical to the pre-refactor router.
    deep = deep_param if deep_param is not None else settings.kg_http_deep_default
    deep_source = "client" if deep_param is not None else "env"
    if deep:
        check_deep_rate_limit(user.username)
        log_kg_deep_access(user.username, "get_project", slug)
        kg_ctx = await build_kg_context_for_project(db, slug, deep=True)
        if kg_ctx is not None:
            kg_ctx.setdefault("meta", {})
            kg_ctx["meta"]["deep_effective"] = deep
            kg_ctx["meta"]["deep_default_source"] = deep_source
        detail.kg_context = kg_ctx

    return detail


@router.patch("/{slug}")
async def patch_project(
    body: ProjectUpdateRequest,
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> ProjectDetail:
    """Update additive project GUI metadata."""
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.update_project(
            ctx,
            db,
            slug=slug,
            color=body.color,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/{slug}/brief")
async def get_session_brief(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    deep: bool = Query(
        False,
        description="False (default): 5 items per KG bucket. True: 10-15 items — heavier, for cold-start or deep debug.",
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Pre-assembled context bundle for agent cold-start.

    Collapses 5+ sequential MCP calls into a single round-trip. Phase 6.5 B
    adds a ``kg_context`` section (hotspots, recent active nodes, cross-project
    mentions, applicable learnings) gated by ``deep`` for effort budget.

    Target latency: <500ms standard, <1000ms with ``deep=true``.
    """
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.get_session_brief(
            ctx, db, slug=slug, deep=deep, visible_projects=visible_projects
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/{slug}/handoffs")
async def get_handoffs(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[HandoffEntry]:
    """List handoff files for a project."""
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.get_handoffs(ctx, db, slug=slug, visible_projects=visible_projects)
    except ServiceError as e:
        raise to_http(e)


@router.get("/{slug}/status-updates")
async def list_project_status_updates_feed(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    limit: int = Query(20, ge=1, le=50),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> StatusUpdateFeedResponse:
    """Feed-style status updates for /projects/detail single-pager v2.

    Merges persisted rows from `project_status_updates` with on-the-fly
    derived entries from recent handoffs (memory/handoff-*.md) and git
    commits (repo_path). Read access is gated by project visibility.
    """
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.list_status_updates_feed(
            ctx, db, slug=slug, limit=limit, visible_projects=visible_projects
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("/{slug}/status-updates", status_code=201)
async def create_project_status_update_feed(
    body: StatusUpdateFeedCreateRequest,
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(require_role("operator")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> StatusUpdateFeedItem:
    """Create a manual feed entry for a project (operator+)."""
    visible_projects = await get_visible_projects(db, user)
    author_display = getattr(user, "display_name", None) or user.username
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.create_status_update_feed(
            ctx,
            db,
            slug=slug,
            content_md=body.content_md,
            author_display=author_display,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/{slug}/plans")
async def get_plans(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[DocEntry]:
    """List all docs for a project (iterates all subdirs of docs/ dynamically)."""
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.get_plans(ctx, db, slug=slug, visible_projects=visible_projects)
    except ServiceError as e:
        raise to_http(e)


@router.get("/{slug}/git/log")
async def get_git_log(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    limit: int = Query(20, ge=1, le=100),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    """Get git log for a project."""
    repo = await _resolve_git_repo(slug, user, db)
    return await _git_log(repo, limit)


@router.get("/{slug}/git/diff")
async def get_git_diff(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get git diff for a project."""
    repo = await _resolve_git_repo(slug, user, db)
    return {"diff": await _git_diff(repo)}


@router.get("/{slug}/git/branches")
async def get_git_branches(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    """Get git branches for a project."""
    repo = await _resolve_git_repo(slug, user, db)
    return await _git_branches(repo)


@router.post("/{slug}/git/push")
async def git_push(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Git push (user only, requires confirmation)."""
    repo = await _resolve_git_repo(slug, user, db)
    await _validate_git_remote(repo)
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "push",
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        error_msg = re.sub(r"https?://[^@]*@", "https://***@", stderr.decode().strip())
        return {"success": False, "error": error_msg}
    return {"success": True, "output": stdout.decode().strip()}


@router.post("/{slug}/git/pull")
async def git_pull(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Git pull (user only)."""
    repo = await _resolve_git_repo(slug, user, db)
    await _validate_git_remote(repo)
    proc = await asyncio.create_subprocess_exec(
        *_GIT_CMD, "pull",
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        error_msg = re.sub(r"https?://[^@]*@", "https://***@", stderr.decode().strip())
        return {"success": False, "error": error_msg}
    return {"success": True, "output": stdout.decode().strip()}


@router.get("/{slug}/git/graph")
async def get_git_graph(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    all_branches: bool = Query(True),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get git graph data: commits with parent hashes + refs for visualization."""
    repo = await _resolve_git_repo(slug, user, db)
    commits, refs = await asyncio.gather(
        _git_graph_log(repo, limit, skip, all_branches),
        _git_refs(repo),
    )
    return {"commits": commits, "refs": refs, "has_more": len(commits) == limit}


@router.get("/{slug}/git/commit/{commit_hash}")
async def get_commit_detail(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    commit_hash: str = PathParam(..., pattern=r"^[a-f0-9]{7,40}$"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get detailed info for a single commit (full message, files changed)."""
    repo = await _resolve_git_repo(slug, user, db)
    result = await _git_commit_detail(repo, commit_hash)
    if result is None:
        raise HTTPException(404, "Commit not found")
    return result
