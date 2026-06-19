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
from pydantic import BaseModel

from core.api.config import settings
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


class LearningHistoryLink(BaseModel):
    """One row in a learning's supersede chain (audit-trail surface, Track 2 #1).

    ``valid_from`` / ``invalid_at`` are the bi-temporal system-time bounds (mig 148);
    ``superseded_by`` points at the next live link; ``supersede_reason`` is the
    human-readable why. ``live`` is the convenience flag ``invalid_at IS NULL``.
    """

    id: str
    title: str
    valid_from: str | None
    invalid_at: str | None
    superseded_by: str | None
    supersede_reason: str | None
    live: bool


class LearningHistoryResponse(BaseModel):
    learning_id: str
    chain: list[LearningHistoryLink]
    count: int


# ---------------------------------------------------------------------------
# Temporal (bi-temporal) read filter — Track 2 #1-S2
# ---------------------------------------------------------------------------


def _temporal_filter(as_of: str | None) -> tuple[str, list[str]]:
    """Return the SQL fragment + params that scope a learnings read to live rows.

    MECHANICAL + BINARY (the spec's CONSUMPTION model): superseded rows are
    EXCLUDED, never down-weighted — a down-weighted tombstone would still be
    visible to the LLM and could be cited, defeating the purpose.

    Behaviour, keyed off ``settings.temporal_memory_enabled`` (alias
    ``MARVIS_TEMPORAL_MEMORY``, DEFAULT False):

    * flag OFF → ``("", [])`` — NO extra SQL. Every read is byte-for-byte
      unchanged (no ``invalid_at`` filter, no ``as_of``). Pre-migration safe.
    * flag ON, no ``as_of`` → ``("AND invalid_at IS NULL", [])`` — only live rows.
    * flag ON, ``as_of`` given → point-in-time window
      ``valid_from <= as_of AND (invalid_at IS NULL OR invalid_at > as_of)``.

    The fragment is meant to be appended to a WHERE that already has at least one
    condition (it starts with ``AND``); the callers all build such a WHERE.
    """
    if not settings.temporal_memory_enabled:
        return "", []
    if as_of is None:
        return "AND invalid_at IS NULL", []
    return (
        "AND valid_from <= ? AND (invalid_at IS NULL OR invalid_at > ?)",
        [as_of, as_of],
    )


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
    as_of: str | None = None,
) -> list[aiosqlite.Row]:
    temporal_sql, temporal_params = _temporal_filter(as_of)

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
        # Temporal scope (Track 2 #1-S2): empty string when the flag is OFF →
        # byte-identical SQL. Its params trail the pattern params so positional
        # binding stays aligned.
        if temporal_sql:
            where = f"{where} {temporal_sql}"
            params.extend(temporal_params)
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
    as_of: str | None = None,
) -> LearningCheckResponse:
    """Search learnings relevant to a keyword/module (any authenticated caller).

    Returns the core results with ``kg_context=None``; the adapter attaches KG
    context when ``deep=true`` (DECISION 2).

    Temporal (Track 2 #1-S2): with ``MARVIS_TEMPORAL_MEMORY`` ON, superseded rows
    are excluded by default (``invalid_at IS NULL``); ``as_of=<ISO>`` reconstructs
    the point-in-time view. Flag OFF → unchanged.
    """
    rows = await _search_learning_rows(db, ctx.workspace_id, query, module, as_of)
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
    as_of: str | None = None,
) -> list[LearningResponse]:
    """List learnings with filters (any authenticated caller).

    Visibility: the adapter resolves ``visible_projects`` (DECISION 1) only when a
    ``project`` filter is present; this use_case ENFORCES it. ``None`` means "no
    restriction" (local/MCP surface, or no project filter).

    Temporal (Track 2 #1-S2): with ``MARVIS_TEMPORAL_MEMORY`` ON, superseded rows
    are excluded by default; ``as_of=<ISO>`` relaxes to the point-in-time window.
    Flag OFF → unchanged.
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

    # Temporal scope (Track 2 #1-S2): appended last so its params trail the
    # filter params and precede LIMIT/OFFSET. Empty when the flag is OFF.
    temporal_sql, temporal_params = _temporal_filter(as_of)
    if temporal_sql:
        where = f"{where} {temporal_sql}"
        params.extend(temporal_params)

    # NOTE: ORDER BY (salience/recency) runs AFTER the live filter above, so
    # tombstones can never bubble into the result via ranking.
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
    as_of: str | None = None,
) -> LearningResponse:
    """Get a single learning by ID, scoped to the caller's workspace.

    Returns ``kg_context=None``; the adapter attaches KG context on ``deep=true``.

    Temporal (Track 2 #1-S2): with ``MARVIS_TEMPORAL_MEMORY`` ON, a superseded row
    is NOT returned by default (raises :class:`NotFoundError`); ``as_of=<ISO>``
    returns it iff it was live at that instant. Flag OFF → unchanged (always
    returns the row regardless of ``invalid_at``). ``get_learning_history`` is the
    explicit audit surface that always walks the chain irrespective of liveness.
    """
    temporal_sql, temporal_params = _temporal_filter(as_of)
    sql = (
        "SELECT * FROM learnings WHERE id = ? "
        "AND COALESCE(workspace_id, 'ws_default') = ? "
        f"{temporal_sql}"
    )
    cursor = await db.execute(
        sql,
        (learning_id, ctx.workspace_id, *temporal_params),
    )
    row = await cursor.fetchone()
    if not row:
        raise NotFoundError(code="learning_not_found", message="Learning not found")
    return _row_to_learning(row)


async def get_learning_history(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    learning_id: str,
) -> LearningHistoryResponse:
    """Walk the supersede chain for a learning (READ-ONLY audit trail, Track 2 #1).

    Starts at ``learning_id`` and follows ``superseded_by`` link by link, emitting
    ``valid_from`` / ``invalid_at`` / ``supersede_reason`` per row. Returns every
    link REGARDLESS of liveness or the ``MARVIS_TEMPORAL_MEMORY`` flag — this is
    the deliberate, opt-in audit surface, the one place tombstones are meant to be
    visible ("what was the convention about X, and when/why did it change?").

    Cycle-safe (a ``superseded_by`` loop terminates via the visited set) and
    workspace-scoped. Raises :class:`NotFoundError` if the head row is absent.
    Never writes.
    """
    chain: list[LearningHistoryLink] = []
    visited: set[str] = set()
    cur_id: str | None = learning_id

    while cur_id is not None and cur_id not in visited:
        visited.add(cur_id)
        cursor = await db.execute(
            "SELECT id, title, valid_from, invalid_at, superseded_by, supersede_reason "
            "FROM learnings WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
            (cur_id, ctx.workspace_id),
        )
        row = await cursor.fetchone()
        if row is None:
            break
        chain.append(
            LearningHistoryLink(
                id=row["id"],
                title=row["title"],
                valid_from=row["valid_from"],
                invalid_at=row["invalid_at"],
                superseded_by=row["superseded_by"],
                supersede_reason=row["supersede_reason"],
                live=row["invalid_at"] is None,
            )
        )
        cur_id = row["superseded_by"]

    if not chain:
        raise NotFoundError(code="learning_not_found", message="Learning not found")

    return LearningHistoryResponse(
        learning_id=learning_id,
        chain=chain,
        count=len(chain),
    )


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
    """Create a new learning (operator+).

    Temporal write-time decision (Track 2 #1-S3, GUARDED by
    ``MARVIS_TEMPORAL_MEMORY``): when the flag is ON, after the live row is
    inserted, the new row's embedding (read back from the embed-on-create mirror —
    NO model run here) is compared cosine-two-band against the top-k live
    neighbours. NOOP near-duplicates skip the row; mid-band (0.80-0.97) matches get
    a pending SUPERSEDE_CANDIDATE proposal written into the existing
    ``brain_memory_operations`` approval gate (the OLD row is NOT invalidated —
    that happens only on human approval). When the flag is OFF this is byte-for-byte
    the old behaviour: NO neighbour fetch, NO decision, always a plain insert.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    _validate_category(category)
    _validate_severity(severity)

    learning_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps(tags or [])

    # ``valid_from`` (mig 148) is written ONLY when the temporal feature is on.
    # With the flag off this is byte-for-byte the pre-148 INSERT, so a brain that
    # has not yet applied migration 148 (e.g. one just upgraded to 0.3.8b1) can
    # still capture learnings — the flag-off path consumes none of the bitemporal
    # columns. (#12)
    cols = (
        "id, title, category, description, tags, module, severity, frequency, "
        "last_occurrence, prevention, session, project, created_at, updated_at"
    )
    placeholders = "?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?"
    values = [
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
    ]
    if settings.temporal_memory_enabled:
        cols += ", valid_from"
        placeholders += ", ?"
        values.append(now)  # valid_from = system-time the fact was learned (mig 148)

    await db.execute(
        f"INSERT INTO learnings ({cols}) VALUES ({placeholders})",
        tuple(values),
    )
    await db.commit()

    # Track 2 #1-S3: write-time consolidation decision. Flag OFF → skipped entirely
    # (the line above is the only temporal change, and valid_from is an additive
    # column read by nothing when the flag is off). Best-effort + non-fatal: a
    # missing embedding mirror (embed still in flight) or any error falls back to
    # the plain ADD already performed above; the dream cycle catches misses later.
    if settings.temporal_memory_enabled:
        await _decide_write_time(db, ctx.workspace_id, learning_id, title, project)

    cursor = await db.execute("SELECT * FROM learnings WHERE id = ?", (learning_id,))
    row = await cursor.fetchone()
    return _row_to_learning(row)


async def _decide_write_time(
    db: aiosqlite.Connection,
    workspace_id: str,
    learning_id: str,
    title: str,
    project: str | None,
) -> None:
    """Run the write-time temporal decision for a just-inserted learning.

    GUARDED by the caller behind ``settings.temporal_memory_enabled``. The row is
    ALREADY inserted live (ADD); this only layers NOOP/SUPERSEDE_CANDIDATE on top:

    * vector unavailable → no-op (stay ADD — embed not mirrored yet / embedder off).
    * ADD verdict        → no-op (already the live row).
    * NOOP verdict       → REINFORCE the matched live neighbour (bump frequency),
                           then soft-remove the just-created duplicate row + its
                           mirror so no near-duplicate fact lingers.
    * SUPERSEDE_CANDIDATE → write a pending proposal into the approval gate; the
                           OLD row is NOT invalidated here (human confirms).

    Never raises into the create path — the create already succeeded.
    """
    from core.api.services import temporal_write as tw

    try:
        new_vec = await tw.fetch_learning_vector(db, learning_id)
        if new_vec is None:
            return  # embedding mirror not present yet → stay ADD
        neighbors = await tw.fetch_live_neighbor_vectors(
            db, workspace_id, new_vec, exclude_learning_id=learning_id
        )
        decision = tw.decide_write_action(new_vec, neighbors)

        if decision.action is tw.WriteAction.ADD or decision.neighbor_id is None:
            return

        if decision.action is tw.WriteAction.NOOP:
            # REINFORCE the matched live neighbour, then retire the duplicate just
            # created (hard-remove a brand-new exact-dup row is safe: it never had
            # downstream references; this is NOT a supersede/soft-invalidate).
            await db.execute(
                "UPDATE learnings SET frequency = frequency + 1, last_occurrence = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    decision.neighbor_id,
                ),
            )
            await _prune_search_index(db, learning_id)
            await db.execute("DELETE FROM learnings WHERE id = ?", (learning_id,))
            await db.commit()
            return

        # SUPERSEDE_CANDIDATE band (0.80-0.97). Track 2 #1-S4: route through the
        # tiebreak RESOLVER seam. The DEFAULT resolver is NoopResolver (loads
        # nothing) → always UNDECIDED → resolve_band → "propose" → IDENTICAL to S3.
        # Only a future OFF-HOST, eval-gated resolver returning a high-confidence
        # SUPERSEDE flips this to "apply" → automatic apply_supersede. No model runs
        # here; resolve_band is a pure decision.
        from core.api.services import temporal_tiebreak as tb

        score = decision.score if decision.score is not None else 0.0
        new_text = title
        neighbor_text = await _fetch_learning_title(db, decision.neighbor_id)
        resolver = tb.get_tiebreak_resolver()
        verdict = resolver.resolve(new_text, neighbor_text, score)

        if tb.resolve_band(verdict) == "apply":
            # High-confidence SUPERSEDE auto-resolved by an off-host judge: retire
            # the OLD row (soft-invalidate) pointing it at the new live row.
            await tw.apply_supersede(
                db,
                old_id=decision.neighbor_id,
                new_id=learning_id,
                reason=(
                    f"write-time auto-supersede (cosine={score:.4f}, "
                    f"confidence={verdict.confidence:.2f})"
                ),
            )
            await db.commit()
            return

        # propose only (old row stays live until approval) — the S3 default path.
        await tw.propose_supersede_candidate(
            db,
            old_id=decision.neighbor_id,
            new_id=learning_id,
            score=score,
            summary=(
                f"Write-time near-match for learning '{title[:80]}' "
                f"(cosine={score:.4f}) — confirm supersede or keep both."
            ),
            project=project,
        )
        await db.commit()
    except Exception:  # pragma: no cover - defensive; create already succeeded
        # The plain ADD already committed; a decision failure must not 500 the create.
        return


async def _fetch_learning_title(
    db: aiosqlite.Connection, learning_id: str
) -> str:
    """Read a learning's title (the text the tiebreak resolver compares against).

    Returns ``""`` if the row is gone — the DEFAULT NoopResolver ignores its inputs,
    so this only matters once a real off-host resolver is wired in.
    """
    cur = await db.execute("SELECT title FROM learnings WHERE id = ?", (learning_id,))
    row = await cur.fetchone()
    if row is None:
        return ""
    return row["title"] or ""


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


async def _prune_search_index(db: aiosqlite.Connection, learning_id: str) -> None:
    """Remove a learning's mirror from the search index, ON THE PASSED WRITE CONNECTION.

    A learning is mirrored into the search index keyed by ``file_path =
    f"learning:{learning_id}"`` (see ``embedding_service.embed_learning_document``).
    Deleting the ``documents`` row auto-cleans the FTS index: the ``documents_fts_delete``
    trigger (migration 136) fires ``AFTER DELETE ON documents`` and removes the matching
    ``documents_fts`` row. Only ``vec_documents`` (the sqlite-vec KNN table) needs an
    explicit delete, and it requires vec0 loaded on the connection — guarded via
    ``ensure_vec_documents`` the same way ``embed_learning_document`` does.

    All work runs on the connection the router passes in. The single-writer
    ``asyncio.Lock`` is NOT reentrant (learning 6130bc49 / f83f5209), so this never
    calls ``write_db()`` again nor any embed helper that re-acquires the writer.
    """
    file_path = f"learning:{learning_id}"
    cur = await db.execute(
        "SELECT id FROM documents WHERE file_path = ?",
        [file_path],
    )
    doc_row = await cur.fetchone()
    if doc_row is None:
        return  # never mirrored (embedder was unavailable) — nothing to prune

    doc_id = doc_row["id"]

    # vec_documents is a sqlite-vec virtual table; only touch it when vec0 is loaded
    # on this connection. ensure_vec_documents returns False when the extension isn't
    # available (e.g. test env without the loadable) — skip the vec delete then, mirroring
    # embed_learning_document's guard. The documents DELETE below still fires the FTS trigger.
    from core.api.db import ensure_vec_documents

    if await ensure_vec_documents(db):
        await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])

    # Deleting the documents row fires documents_fts_delete (migration 136) automatically.
    await db.execute("DELETE FROM documents WHERE id = ?", [doc_id])


async def delete_learning(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    learning_id: str,
) -> None:
    """Permanently delete a learning + prune its search index mirror (operator+).

    Hard delete by design — there is no soft-delete column on ``learnings``. Verifies
    the learning exists in the caller's workspace (raises :class:`NotFoundError` -> 404
    otherwise, never a 500), then prunes the search index (documents + vec_documents,
    with FTS auto-cleaned by the DB trigger) and deletes the row, ALL on the passed
    write connection.

    The use_case receives a WRITE connection (the router passes ``Depends(get_write_db)``);
    it never re-acquires the non-reentrant single-writer lock.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    cursor = await db.execute(
        "SELECT id FROM learnings WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        (learning_id, ctx.workspace_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise NotFoundError(code="learning_not_found", message="Learning not found")

    # 1. Prune the search-index mirror (documents + vec_documents; FTS via trigger).
    await _prune_search_index(db, learning_id)

    # 2. Delete the learning itself (workspace-scoped, mirroring the other use_cases).
    await db.execute(
        "DELETE FROM learnings WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        (learning_id, ctx.workspace_id),
    )
    await db.commit()


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
