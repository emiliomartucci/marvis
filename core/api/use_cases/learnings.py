# v1.0.0 - 2026-05-27 - S1 F1.1: learnings use_cases extracted from router (TEMPLATE router)
"""Learnings use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

This module is the **template** for the S1 "collapse runtime" refactor: every
router gets a sibling ``use_cases/<name>.py`` with one pure async function per
operation, signature ``(ctx: CallerContext, db, *typed_args) -> <DTO>``. Routers
become thin adapters that resolve identity into a :class:`CallerContext`, call
these functions, and map :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http``. The Python MCP surface (later) calls the SAME
functions with ``CallerContext.local_single_user()``. One implementation, no fork.

Three template decisions are baked in here (replicated across all 15 routers):

DECISION 1 — Visibility resolution at the adapter, enforcement in the use_case.
    ``get_visible_projects`` needs ``UserInfo.teams``, a field NOT carried by
    ``CallerContext`` (by design — identity-expansion stays at the transport
    boundary). So the ADAPTER resolves ``visible_projects`` (only when a
    ``project`` filter is given) and passes it as a keyword arg; this use_case
    only ENFORCES it (``project not in visible_projects -> AuthorizationError``).
    The MCP/local surface passes ``visible_projects=None`` (local operator sees
    all). This module never imports ``get_visible_projects``. Call it
    "the visibility template".

DECISION 2 — ``deep`` KG enrichment is a per-surface adapter concern.
    The ``deep=true`` path uses ``check_deep_rate_limit`` / ``log_kg_deep_access``
    (``services/kg/audit.py``, scheduled for ServiceError conversion in a LATER
    phase) and ``build_kg_context_for_learning`` (``services/kg/lens.py``).
    Rate-limiting + access logging are transport concerns, so the use_case
    returns the core learning(s) with ``kg_context=None`` and the adapter, when
    ``deep=true``, performs rate-limit + log + attaches ``kg_context``. Behavior
    is identical to today. This module never imports the kg services.

DECISION 3 — the ``deep_requires_filter`` guard stays in the adapter.
    Because ``deep`` enrichment lives in the adapter (D2), the guard that rejects
    ``deep`` without a filter is ALSO an adapter concern. The router keeps raising
    the exact ``HTTPException(400, ...)`` (NOT routed through ServiceError, to
    preserve the 400 status — only domain errors flow through ServiceError).

Net effect of D2+D3: the use_case is pure CRUD/search/validation/RBAC/visibility;
everything ``deep``-related (guard + rate-limit + enrichment) stays in the adapter.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import aiosqlite
from pydantic import BaseModel, Field

from core.api.use_cases._context import CallerContext, require_role_ctx
from core.api.use_cases._errors import (
    AuthorizationError,
    NotFoundError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {
    "deploy",
    "migration",
    "auth",
    "testing",
    "architecture",
    "security",
    "performance",
}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
CHECK_LEARNINGS_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "under",
    "after",
    "before",
    "using",
    "used",
    "use",
    "still",
    "real",
    "true",
    "false",
    "why",
    "how",
    "where",
    "when",
    "all",
    "too",
    "but",
    "not",
    "are",
    "was",
    "were",
    "been",
    "have",
    "has",
    "had",
    "into",
    "between",
    "without",
    "always",
    "there",
    "them",
    "their",
    "about",
    "because",
    "serve",
}


# ---------------------------------------------------------------------------
# Domain DTOs (Pydantic is allowed in use_cases — only ``fastapi`` is forbidden)
# ---------------------------------------------------------------------------


class LearningResponse(BaseModel):
    id: str
    title: str
    category: str
    description: str
    tags: list[str]
    module: str | None
    severity: str
    frequency: int
    last_occurrence: str | None
    prevention: str | None
    session: int | None
    project: str | None
    created_at: str
    updated_at: str | None
    kg_context: dict | None = None  # populated by the adapter when ?deep=true (D2)


class LearningCheckResponse(BaseModel):
    query: str
    module: str | None
    results: list[LearningResponse]
    count: int


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _row_to_learning(row: aiosqlite.Row) -> LearningResponse:
    tags_raw = row["tags"] or "[]"
    try:
        tags = json.loads(tags_raw)
    except (json.JSONDecodeError, TypeError):
        tags = []
    return LearningResponse(
        id=row["id"],
        title=row["title"],
        category=row["category"],
        description=row["description"],
        tags=tags,
        module=row["module"],
        severity=row["severity"],
        frequency=row["frequency"],
        last_occurrence=row["last_occurrence"],
        prevention=row["prevention"],
        session=row["session"],
        project=row["project"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _extract_check_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9_./-]+", query.lower()):
        cleaned = token.strip("._-/")
        if len(cleaned) < 2 or cleaned in CHECK_LEARNINGS_STOPWORDS:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        terms.append(cleaned)
    return terms[:8]


def _learning_match_score(row: aiosqlite.Row, patterns: list[str]) -> int:
    haystack = " ".join(
        [
            row["title"] or "",
            row["description"] or "",
            row["tags"] or "",
            row["module"] or "",
            row["prevention"] or "",
        ]
    ).lower()
    return sum(haystack.count(pattern.lower()) for pattern in patterns)


async def _search_learning_rows(
    db: aiosqlite.Connection,
    workspace_id: str,
    query: str,
    module: str | None,
) -> list[aiosqlite.Row]:
    def _base_params(patterns: list[str]) -> tuple[str, list[str]]:
        conditions: list[str] = ["COALESCE(workspace_id, 'ws_default') = ?"]
        params: list[str] = [workspace_id]

        if module:
            conditions.append("module LIKE ?")
            params.append(f"%{module}%")

        per_pattern: list[str] = []
        for pattern in patterns:
            keyword = f"%{pattern}%"
            per_pattern.append(
                "(title LIKE ? OR description LIKE ? OR tags LIKE ? OR module LIKE ? OR prevention LIKE ?)"
            )
            params.extend([keyword, keyword, keyword, keyword, keyword])

        conditions.append(f"({' OR '.join(per_pattern)})")
        where = " AND ".join(conditions)
        return where, params

    exact_where, exact_params = _base_params([query])
    sql = (
        f"SELECT * FROM learnings WHERE {exact_where} "
        f"ORDER BY frequency DESC, severity = 'critical' DESC, severity = 'high' DESC, "
        f"last_occurrence DESC NULLS LAST LIMIT 20"
    )
    rows = await (await db.execute(sql, exact_params)).fetchall()
    if rows:
        return rows

    fallback_terms = _extract_check_terms(query)
    if not fallback_terms or fallback_terms == [query.lower()]:
        return rows

    fallback_where, fallback_params = _base_params(fallback_terms)
    fallback_sql = (
        f"SELECT * FROM learnings WHERE {fallback_where} "
        f"ORDER BY frequency DESC, severity = 'critical' DESC, severity = 'high' DESC, "
        f"last_occurrence DESC NULLS LAST LIMIT 20"
    )
    fallback_rows = await (await db.execute(fallback_sql, fallback_params)).fetchall()
    return sorted(
        fallback_rows,
        key=lambda row: (
            _learning_match_score(row, fallback_terms),
            row["frequency"] or 0,
            row["last_occurrence"] or "",
        ),
        reverse=True,
    )


def _validate_category(category: str) -> None:
    if category not in VALID_CATEGORIES:
        raise ValidationError(
            code="invalid_category",
            message=f"Invalid category '{category}'. Valid: {', '.join(sorted(VALID_CATEGORIES))}",
        )


def _validate_severity(severity: str) -> None:
    if severity not in VALID_SEVERITIES:
        raise ValidationError(
            code="invalid_severity",
            message=f"Invalid severity '{severity}'. Valid: {', '.join(sorted(VALID_SEVERITIES))}",
        )


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def check_learnings(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    query: str,
    module: str | None = None,
) -> LearningCheckResponse:
    """Search learnings relevant to a keyword/module (any authenticated caller).

    Returns the core results with ``kg_context=None``; the adapter attaches KG
    context when ``deep=true`` (DECISION 2).
    """
    rows = await _search_learning_rows(db, ctx.workspace_id, query, module)
    results = [_row_to_learning(row) for row in rows]
    return LearningCheckResponse(
        query=query,
        module=module,
        results=results,
        count=len(results),
    )


async def list_learnings(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    category: str | None = None,
    project: str | None = None,
    severity: str | None = None,
    tags: str | None = None,
    search: str | None = None,
    module: str | None = None,
    limit: int = 50,
    offset: int = 0,
    visible_projects: set[str] | None = None,
) -> list[LearningResponse]:
    """List learnings with filters (any authenticated caller).

    Visibility: the adapter resolves ``visible_projects`` (DECISION 1) only when a
    ``project`` filter is present; this use_case ENFORCES it. ``None`` means "no
    restriction" (local/MCP surface, or no project filter).
    """
    if project and visible_projects is not None and project not in visible_projects:
        raise AuthorizationError(
            code="project_not_accessible",
            message="Project not accessible",
        )

    conditions: list[str] = []
    params: list[str] = []

    # Workspace isolation
    conditions.append("COALESCE(workspace_id, 'ws_default') = ?")
    params.append(ctx.workspace_id)

    if category:
        conditions.append("category = ?")
        params.append(category)

    if project:
        conditions.append("project = ?")
        params.append(project)

    if severity:
        conditions.append("severity = ?")
        params.append(severity)

    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        tag_conditions = []
        for tag in tag_list:
            tag_conditions.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        conditions.append(f"({' OR '.join(tag_conditions)})")

    if search:
        keyword = f"%{search}%"
        conditions.append("(title LIKE ? OR description LIKE ? OR prevention LIKE ?)")
        params.extend([keyword, keyword, keyword])

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = (
        f"SELECT * FROM learnings WHERE {where} "
        f"ORDER BY frequency DESC, created_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([str(limit), str(offset)])

    cursor = await db.execute(sql, params)
    return [_row_to_learning(row) async for row in cursor]


async def get_learning(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    learning_id: str,
) -> LearningResponse:
    """Get a single learning by ID, scoped to the caller's workspace.

    Returns ``kg_context=None``; the adapter attaches KG context on ``deep=true``.
    """
    cursor = await db.execute(
        "SELECT * FROM learnings WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        (learning_id, ctx.workspace_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise NotFoundError(code="learning_not_found", message="Learning not found")
    return _row_to_learning(row)


async def create_learning(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    title: str,
    category: str,
    description: str,
    tags: list[str] | None = None,
    module: str | None = None,
    severity: str = "medium",
    prevention: str | None = None,
    session: int | None = None,
    project: str | None = None,
) -> LearningResponse:
    """Create a new learning (operator+)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    _validate_category(category)
    _validate_severity(severity)

    learning_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps(tags or [])

    await db.execute(
        "INSERT INTO learnings (id, title, category, description, tags, module, "
        "severity, frequency, last_occurrence, prevention, session, project, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            learning_id,
            title,
            category,
            description,
            tags_json,
            module,
            severity,
            now,
            prevention,
            session,
            project,
            now,
            now,
        ),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM learnings WHERE id = ?", (learning_id,))
    row = await cursor.fetchone()
    return _row_to_learning(row)


async def update_learning(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    learning_id: str,
    fields: dict,
) -> LearningResponse:
    """Update a learning (operator+).

    ``fields`` carries only the keys the caller actually set (the adapter passes
    ``request.model_dump(exclude_unset=True)``), preserving the HTTP semantic
    where omitting a field leaves it unchanged and passing ``None`` clears it.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    cursor = await db.execute(
        "SELECT * FROM learnings WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        (learning_id, ctx.workspace_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise NotFoundError(code="learning_not_found", message="Learning not found")

    updates: dict[str, str | int | None] = {}

    if "title" in fields:
        updates["title"] = fields["title"]
    if "category" in fields:
        _validate_category(fields["category"])
        updates["category"] = fields["category"]
    if "description" in fields:
        updates["description"] = fields["description"]
    if "tags" in fields:
        updates["tags"] = json.dumps(fields["tags"]) if fields["tags"] is not None else None
    if "module" in fields:
        updates["module"] = fields["module"]
    if "severity" in fields:
        _validate_severity(fields["severity"])
        updates["severity"] = fields["severity"]
    if "prevention" in fields:
        updates["prevention"] = fields["prevention"]
    if "session" in fields:
        updates["session"] = fields["session"]
    if "project" in fields:
        updates["project"] = fields["project"]

    if not updates:
        raise ValidationError(code="no_fields_to_update", message="No fields to update")

    now = datetime.now(timezone.utc).isoformat()
    updates["updated_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [learning_id]

    await db.execute(f"UPDATE learnings SET {set_clause} WHERE id = ?", values)
    await db.commit()

    cursor = await db.execute("SELECT * FROM learnings WHERE id = ?", (learning_id,))
    row = await cursor.fetchone()
    return _row_to_learning(row)


async def bump_learning(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    learning_id: str,
) -> LearningResponse:
    """Increment frequency + refresh last_occurrence when an issue recurs (operator+)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    cursor = await db.execute(
        "SELECT * FROM learnings WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        (learning_id, ctx.workspace_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise NotFoundError(code="learning_not_found", message="Learning not found")

    now = datetime.now(timezone.utc).isoformat()
    new_frequency = (row["frequency"] or 0) + 1

    await db.execute(
        "UPDATE learnings SET frequency = ?, last_occurrence = ?, updated_at = ? WHERE id = ?",
        (new_frequency, now, now, learning_id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM learnings WHERE id = ?", (learning_id,))
    row = await cursor.fetchone()
    return _row_to_learning(row)
