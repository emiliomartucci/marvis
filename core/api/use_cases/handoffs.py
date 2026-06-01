# v1.0.0 - 2026-05-27 - S1 F1.11: handoffs use_cases extracted from router
"""Handoffs use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Handoffs are project-scoped docs living on disk (``<project>/memory/handoff-*.md``).
This module owns the full-text + semantic search and the single-handoff read,
keyed only off a :class:`CallerContext`, the DB connections, and pre-resolved
``visible_projects``. The router stays a thin adapter (identity -> context,
``ServiceError`` -> HTTP, ``deep`` KG enrichment).

The three template decisions (same as the prior routers) apply here:

DECISION 1 — Visibility resolution at the adapter, enforcement in the use_case.
    ``get_visible_projects`` needs ``UserInfo.teams``, which ``CallerContext`` does
    not carry. The ADAPTER resolves ``visible_projects`` (``set[str]`` of allowed
    slugs, or ``None`` for unrestricted = admin/agent) and passes it in; this
    use_case only ENFORCES it. ``None`` == "no restriction" (admin/agent, or the
    local/MCP single-user surface). This module never imports
    ``get_visible_projects``.

    Status preservation matters and differs per endpoint:
    - ``search_handoffs`` filters silently (a non-visible ``project`` filter or an
      empty visible set yields ``[]`` — never an error), matching the original
      router exactly.
    - ``get_handoff`` raises :class:`NotFoundError` (404) when the project is not
      visible, mirroring ``visibility.check_project_access`` ("404 not 403 — does
      not reveal existence").

DECISION 2 — ``deep`` KG enrichment is a per-surface adapter concern.
    The ``get_handoff(?deep=true)`` path uses ``check_deep_rate_limit`` /
    ``log_kg_deep_access`` (transport-level rate-limit + audit) and
    ``build_kg_context_for_handoff`` (``services/kg/lens.py``). The use_case returns
    the handoff with ``kg_context=None``; the adapter, when ``deep=true``, performs
    rate-limit + log + attaches ``kg_context``. This module never imports the kg
    services. Behavior is identical to today.

DECISION 3 (function-local service imports) — same as ``search.py``.
    ``embedding_service`` (fastapi-free) and ``routers.projects`` (which DOES import
    ``fastapi`` at module level, but exposes the project-index/path infrastructure
    + ``MAX_FILE_SIZE`` + ``_SLUG_RE`` reused here) are imported FUNCTION-LOCAL so
    this module stays fastapi-free at import time — the property the import-linter
    contract (``use_cases-no-fastapi``) and the smoke test assert.
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date
from pathlib import Path

import aiosqlite
import yaml

from core.api.models import HandoffSearchResult
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import (
    NotFoundError,
    ServiceError,
    ServiceUnavailableError,
)

logger = logging.getLogger(__name__)

# Max chars to return as snippet
_SNIPPET_RADIUS = 150   # chars on each side of match → ~300 total

# Filename validation: allow only handoff-*.md (no path traversal)
_HANDOFF_FILENAME_RE = re.compile(r"^handoff-[\w.\-]+\.md$")


# ---------------------------------------------------------------------------
# Pure helpers (re-exported by the router for existing importers)
# ---------------------------------------------------------------------------


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from file content.

    Returns (frontmatter_dict, body_after_frontmatter).
    Body starts right after the closing '---' delimiter.
    """
    if not content.startswith("---"):
        return {}, content
    end = content.find("---", 3)
    if end < 0:
        return {}, content
    try:
        fm = yaml.safe_load(content[3:end]) or {}
        if not isinstance(fm, dict):
            fm = {}
    except Exception:
        fm = {}
    body = content[end + 3:].lstrip("\n")
    return fm, body


def _extract_snippet(body: str, query_lower: str, radius: int = _SNIPPET_RADIUS) -> str:
    """Return a snippet of ~2*radius chars centred on the first match of query in body.

    Falls back to the first 300 chars of body when query is empty or not found.
    """
    if not query_lower or not body:
        return body[:300].strip()
    idx = body.lower().find(query_lower)
    if idx < 0:
        return body[:300].strip()
    start = max(0, idx - radius)
    end = min(len(body), idx + len(query_lower) + radius)
    snippet = body[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(body):
        snippet = snippet + "..."
    return snippet


def _count_matches(text: str, query_lower: str) -> int:
    """Count case-insensitive occurrences of query_lower in text."""
    if not query_lower:
        return 0
    return text.lower().count(query_lower)


def _safe_read(path: Path) -> str | None:
    """Read file with size guard; returns None on failure."""
    from core.api.routers.projects import MAX_FILE_SIZE

    if path.is_symlink():
        return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _iter_all_slugs() -> list[str]:
    """Return all known project slugs, rebuilding the index if stale."""
    import time

    import core.api.routers.projects as _projects_mod

    if not _projects_mod._project_index or (
        time.monotonic() - _projects_mod._index_built_at > _projects_mod._INDEX_TTL
    ):
        _projects_mod._build_project_index()
    return list(_projects_mod._project_index.keys())


# ---------------------------------------------------------------------------
# Semantic search helper
# ---------------------------------------------------------------------------


async def _search_semantic(
    q: str,
    vec_db: aiosqlite.Connection,
    visible_slugs: set[str] | None,
    project_filter: str | None,
    limit: int,
) -> list[HandoffSearchResult]:
    """KNN search via sqlite-vec with project post-filter."""
    from core.api.services import embedding_service

    query_embedding = await embedding_service.embed_texts([q], input_type="query")
    vec_bytes = embedding_service.serialize_f32(query_embedding[0])

    # Overcollect: vec0 filters AFTER KNN scan, not before
    overcollect = limit * 3

    rows_raw = await vec_db.execute(
        """
        SELECT d.file_path, d.project, v.distance
        FROM vec_documents v
        JOIN documents d ON d.id = v.doc_id
        WHERE v.embedding MATCH ? AND v.k = ?
        ORDER BY v.distance
        """,
        [vec_bytes, overcollect],
    )
    rows = await rows_raw.fetchall()

    # Post-filter by visibility and project
    results: list[HandoffSearchResult] = []
    for row in rows:
        proj = row["project"]
        if visible_slugs is not None and proj not in visible_slugs:
            continue
        if project_filter and proj != project_filter:
            continue

        fpath = Path(row["file_path"])
        # Extract metadata from file
        content = _safe_read(fpath)
        if not content:
            continue
        fm, body = _parse_frontmatter(content)
        fm_date_raw = str(fm.get("date", "")) or ""
        if not fm_date_raw:
            m = re.search(r"handoff-(\d{4}-\d{2}-\d{2})", fpath.name)
            fm_date_raw = m.group(1) if m else ""
        fm_session = None
        raw_session = fm.get("session")
        if raw_session not in (None, ""):
            fm_session = str(raw_session)
        fm_tags_raw = fm.get("tags", [])
        if isinstance(fm_tags_raw, str):
            fm_tags_raw = [fm_tags_raw]
        fm_tags = [str(t) for t in fm_tags_raw if t]

        results.append(HandoffSearchResult(
            project=proj,
            file=fpath.name,
            date=fm_date_raw or None,
            session=fm_session,
            tags=fm_tags,
            branch=fm.get("branch"),
            snippet=_extract_snippet(body, q.lower()),
            score=round((1.0 - row["distance"]) * 100),
        ))
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def search_handoffs(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    vec_db: aiosqlite.Connection,
    *,
    q: str | None = None,
    project: str | None = None,
    tags: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    limit: int = 20,
    visible_projects: set[str] | None = None,
) -> list[HandoffSearchResult]:
    """Full-text (+ semantic) search across handoff files (any authenticated caller).

    Parses frontmatter YAML from every handoff-*.md file found in project memory/
    directories and filters by query, project, tags, and date range. Results are
    ranked by match count (descending), then date (descending).

    Visibility (DECISION 1): the adapter resolves ``visible_projects`` and passes it
    in; this use_case ENFORCES it by silent filtering (matching the original
    router): an empty visible set or a non-visible ``project`` filter yields ``[]``.
    ``None`` means "no restriction" (admin/agent, or local/MCP surface).
    """
    from core.api.services import embedding_service
    from core.api.routers import projects as _projects_mod

    # --- Team-based visibility filter ---
    visible_slugs = visible_projects
    if visible_slugs is not None and not visible_slugs:
        return []

    # --- Semantic search (if vec0 available, query present, no tag/date filters) ---
    if q and q.strip() and not tags and not date_start and not date_end and embedding_service.is_available():
        try:
            sem_results = await _search_semantic(q.strip(), vec_db, visible_slugs, project, limit)
            if sem_results:
                return sem_results
        except Exception:
            logger.warning("Semantic search failed, falling back to keyword", exc_info=True)

    # --- Normalise inputs ---
    q_lower = q.strip().lower() if q and q.strip() else ""
    tag_filter: list[str] = (
        [t.strip().lower() for t in tags.split(",") if t.strip()] if tags else []
    )

    date_start_d: _date | None = None
    date_end_d: _date | None = None
    try:
        if date_start:
            date_start_d = _date.fromisoformat(date_start)
        if date_end:
            date_end_d = _date.fromisoformat(date_end)
    except ValueError:
        pass  # invalid date string — ignore the filter

    # --- Determine which slugs to scan ---
    if project:
        if not _projects_mod._SLUG_RE.match(project):
            return []
        # Check visibility for the specific project
        if visible_slugs is not None and project not in visible_slugs:
            return []
        slugs = [project]
    else:
        all_slugs = _iter_all_slugs()
        # Apply visibility filter
        if visible_slugs is not None:
            slugs = [s for s in all_slugs if s in visible_slugs]
        else:
            slugs = all_slugs

    results: list[HandoffSearchResult] = []

    for slug in slugs:
        project_path = _projects_mod._find_project_path(slug)
        if not project_path:
            continue

        memory_dir = project_path / "memory"
        if not memory_dir.is_dir():
            continue

        for f in sorted(memory_dir.glob("handoff-*.md"), reverse=True):
            if f.is_symlink():
                continue

            content = _safe_read(f)
            if not content:
                continue

            fm, body = _parse_frontmatter(content)

            # --- Extract frontmatter fields ---
            fm_date_raw = str(fm["date"]) if "date" in fm else ""
            # Fallback: extract date from filename
            if not fm_date_raw:
                m = re.search(r"handoff-(\d{4}-\d{2}-\d{2})", f.name)
                fm_date_raw = m.group(1) if m else ""

            fm_session = None
            raw_session = fm.get("session")
            if raw_session not in (None, ""):
                fm_session = str(raw_session)

            fm_branch: str | None = fm.get("branch")
            fm_tags_raw = fm.get("tags", [])
            if isinstance(fm_tags_raw, str):
                fm_tags_raw = [fm_tags_raw]
            fm_tags: list[str] = [str(t) for t in fm_tags_raw if t]

            # --- Date filter ---
            if date_start_d or date_end_d:
                try:
                    file_date = _date.fromisoformat(fm_date_raw) if fm_date_raw else None
                except ValueError:
                    file_date = None
                if file_date is None:
                    continue
                if date_start_d and file_date < date_start_d:
                    continue
                if date_end_d and file_date > date_end_d:
                    continue

            # --- Tag filter (OR logic) ---
            if tag_filter:
                file_tags_lower = [t.lower() for t in fm_tags]
                if not any(t in file_tags_lower for t in tag_filter):
                    continue

            # --- Full-text filter ---
            if q_lower:
                # Search in: filename, tags (joined), body
                search_corpus = " ".join([
                    f.name.lower(),
                    " ".join(fm_tags).lower(),
                    body.lower(),
                ])
                if q_lower not in search_corpus:
                    continue

            # --- Compute score (match count in full content) ---
            score = _count_matches(
                f.name + " " + " ".join(fm_tags) + " " + body,
                q_lower,
            )

            # --- Extract snippet ---
            snippet = _extract_snippet(body, q_lower)

            results.append(HandoffSearchResult(
                project=slug,
                file=f.name,
                date=fm_date_raw or None,
                session=fm_session,
                tags=fm_tags,
                branch=fm_branch,
                snippet=snippet,
                score=score,
            ))

            if len(results) >= limit * 10:
                # Safety valve: stop scanning once we have enough candidates
                # (will be trimmed after sorting below)
                break

    # Sort: score desc, then date desc (most relevant and most recent first)
    results.sort(key=lambda r: (r.score, r.date or ""), reverse=True)

    return results[:limit]


async def get_handoff(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    filename: str,
    visible_projects: set[str] | None = None,
) -> dict:
    """Get a single handoff file by project slug and filename (any authenticated caller).

    Returns frontmatter (parsed YAML) + body text with ``kg_context=None``; the
    adapter attaches ``kg_context`` (+ rate-limit + audit log) on ``deep=true``
    (DECISION 2).

    Validation/visibility (status-preserving vs the original router):
    - invalid slug / filename -> base :class:`ServiceError` (``http_status = 400``,
      the EXACT 400 the original ``HTTPException(400, ...)`` raised — there is no
      domain subclass for 400, so the base carries the hint, same as ``brain.py``);
    - project not visible -> :class:`NotFoundError` (404), mirroring
      ``check_project_access`` (does not reveal existence) (DECISION 1);
    - project / file missing or unreadable -> :class:`NotFoundError` (404).
    """
    from core.api.routers import projects as _projects_mod

    # --- Input validation (400, not 422 — preserve the original status) ---
    if not _projects_mod._SLUG_RE.match(project_slug):
        raise ServiceError(code="invalid_project_slug", message="Invalid project slug")
    if not _HANDOFF_FILENAME_RE.match(filename):
        raise ServiceError(
            code="invalid_filename",
            message="Invalid filename — must match handoff-*.md",
        )

    # --- Access control (AC-F9) — DECISION 1, 404 not 403 (does not reveal existence) ---
    if visible_projects is not None and project_slug not in visible_projects:
        raise NotFoundError(code="not_found", message="Not found")

    # --- Resolve project path ---
    project_path = _projects_mod._find_project_path(project_slug)
    if not project_path:
        raise NotFoundError(code="project_not_found", message="Project not found on server")

    # --- Resolve file path ---
    file_path = project_path / "memory" / filename
    # Extra containment guard — resolved path must remain inside memory/
    try:
        file_path = file_path.resolve()
        memory_dir = (project_path / "memory").resolve()
        if not str(file_path).startswith(str(memory_dir) + "/") and file_path != memory_dir:
            raise ServiceError(code="invalid_filename", message="Invalid filename")
    except ServiceError:
        raise
    except Exception:
        raise ServiceError(code="invalid_filename", message="Invalid filename")

    if not file_path.is_file():
        raise NotFoundError(
            code="handoff_not_found",
            message=f"Handoff file '{filename}' not found",
        )

    # --- Read + parse ---
    content = _safe_read(file_path)
    if content is None:
        raise NotFoundError(
            code="handoff_not_readable",
            message=f"Handoff file '{filename}' not readable",
        )

    frontmatter, body = _parse_frontmatter(content)

    return {
        "project": project_slug,
        "file": filename,
        "frontmatter": frontmatter,
        "body": body,
        "kg_context": None,  # adapter attaches on deep=true (DECISION 2)
    }


async def reindex_handoffs(
    ctx: CallerContext,
    vec_db: aiosqlite.Connection,
    *,
    project: str,
) -> dict:
    """On-demand reindex of handoff embeddings for a project (admin+).

    RBAC is enforced at the adapter via ``Depends(require_role(...))`` (Bearer-
    accessible for deploy scripts), so this use_case does not re-check the role;
    it only guards on embedding availability (503 when Voyage AI is not configured).
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        raise ServiceUnavailableError(
            code="embeddings_unavailable",
            message="Voyage AI not configured",
        )
    return await embedding_service.reindex_project(project, vec_db)
