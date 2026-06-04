# v1.0.0 - 2026-05-27 - S1 F1.3: costs use_cases extracted from router
"""Costs use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Follows the learnings TEMPLATE (``use_cases/learnings.py``): one pure async
function per operation, signature ``(ctx: CallerContext, db, *typed_args) -> <DTO>``.
Routers stay thin adapters that resolve identity into a :class:`CallerContext`,
call these functions, and map :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http``. The Python MCP surface (later) calls the SAME
functions with ``CallerContext.local_single_user()``.

``costs`` is read-only and isolated, so only two of the template decisions apply:

DECISION 1 — Visibility resolution at the adapter, enforcement in the use_case.
    ``by-project`` and ``billing`` were guarded by ``check_project_access`` which
    calls ``get_visible_projects`` (needs ``UserInfo.teams`` / ``user_id``, fields
    NOT carried by ``CallerContext`` by design). So the ADAPTER resolves
    ``visible_projects`` and passes it as a keyword arg; this use_case only
    ENFORCES it. The contract is a **404** (does NOT reveal project existence), so
    the enforcement raises :class:`NotFoundError` — NOT ``AuthorizationError`` —
    matching ``check_project_access`` exactly. ``visible_projects=None`` means "no
    restriction" (admin/agent, or MCP/local operator). This module never imports
    ``get_visible_projects``.

DECISION 3 (analogue) — date-range normalization + its 400 stay in the adapter.
    ``_resolve_date_range`` auto-fills missing query params and raises
    ``HTTPException(400, ...)`` on an inverted / oversized range. That is input
    normalization at the transport boundary, and the 400 status has no domain
    ``ServiceError`` counterpart (only domain errors flow through ServiceError),
    so the router keeps it. The use_case receives already-resolved
    ``(from_date, to_date)`` ISO strings.

The response DTOs (``ProjectCostSummary`` / ``ConversationCost`` /
``ProjectBillingSummary``) live in ``core.api.models.costs`` and are NOT moved
(unlike the learnings template, where they were router-defined). The program-map
and billing-config lookups are resolved against the router/service modules at
CALL time (``_programs_loader`` / ``cost_service._get_billing_config``) so the
existing monkeypatch points (``api.routers.costs._get_programs`` and
``api.services.cost_service._get_billing_config``) keep working unchanged.
"""
from __future__ import annotations

from typing import Callable

import aiosqlite

from core.api.models import (
    ConversationCost,
    ProjectBillingSummary,
    ProjectCostSummary,
)
from core.api.services import cost_service
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import NotFoundError


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _build_program_map(programs: dict) -> dict[str, str]:
    """Build slug → program name mapping from a ``programs.yaml`` dict.

    The ``programs`` dict is resolved by the adapter (via ``_get_programs``) and
    passed in, so this stays pure and the existing
    ``patch("api.routers.costs._get_programs", ...)`` test seam is unaffected.
    """
    slug_to_program: dict[str, str] = {}
    for program_name, program_data in programs.items():
        if isinstance(program_data, dict):
            for slug in program_data.get("projects", []):
                slug_to_program[slug] = program_name
    return slug_to_program


def _enforce_project_access(
    slug: str, visible_projects: set[str] | None
) -> None:
    """Enforce visibility for a slug-scoped endpoint (DECISION 1).

    Mirrors ``visibility.check_project_access``: ``visible_projects=None`` means
    unrestricted (admin/agent, or MCP/local); otherwise a slug outside the set
    raises a **404** (does NOT reveal existence — same as today).
    """
    if visible_projects is not None and slug not in visible_projects:
        raise NotFoundError(code="project_not_found", message="Not found")


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def get_costs_summary(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    from_date: str,
    to_date: str,
    programs_loader: Callable[[], dict],
) -> list[ProjectCostSummary]:
    """All projects with cost > 0 in the window, grouped by project_slug.

    No slug gate (aggregate view, any authenticated caller). ``programs_loader``
    is invoked here so the adapter's monkeypatch seam keeps working; it returns
    the raw ``programs.yaml`` dict used to attach the program name per slug.
    """
    cursor = await db.execute(
        """SELECT
            COALESCE(project_slug, '__unassigned__') AS slug,
            SUM(cost_usd) AS total_cost,
            COUNT(*) AS conv_count
        FROM session_costs
        WHERE updated_at >= ? AND updated_at < date(?, '+1 day')
        GROUP BY slug
        HAVING total_cost > 0
        ORDER BY total_cost DESC""",
        (from_date, to_date),
    )
    rows = await cursor.fetchall()

    slug_to_program = _build_program_map(programs_loader())

    return [
        ProjectCostSummary(
            project_slug=row["slug"],
            program=slug_to_program.get(row["slug"]),
            total_cost_usd=round(row["total_cost"], 4),
            conversation_count=row["conv_count"],
        )
        for row in rows
    ]


async def get_project_costs(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    from_date: str,
    to_date: str,
    limit: int = 50,
    offset: int = 0,
    visible_projects: set[str] | None = None,
) -> list[ConversationCost]:
    """Conversation costs for a specific project, newest first.

    Visibility: the adapter resolves ``visible_projects`` (DECISION 1); this
    use_case ENFORCES it before any query, raising a 404 when the slug is not
    accessible (does not reveal existence).
    """
    _enforce_project_access(slug, visible_projects)

    cursor = await db.execute(
        """SELECT
            sc.conversation_id,
            sc.session_name,
            sm.display_name,
            sc.model,
            sc.cost_usd,
            sc.input_tokens,
            sc.output_tokens,
            sc.message_count,
            COALESCE(sm.working_seconds, 0) AS working_seconds,
            sm.created_at,
            sc.completed_at,
            sc.updated_at
        FROM session_costs sc
        LEFT JOIN sessions_meta sm ON sm.name = sc.session_name
        WHERE sc.project_slug = ?
        AND sc.updated_at >= ? AND sc.updated_at < date(?, '+1 day')
        ORDER BY sc.updated_at DESC
        LIMIT ? OFFSET ?""",
        (slug, from_date, to_date, limit, offset),
    )
    rows = await cursor.fetchall()

    return [
        ConversationCost(
            conversation_id=row["conversation_id"],
            session_name=row["session_name"],
            display_name=row["display_name"],
            model=row["model"],
            cost_usd=round(row["cost_usd"], 4),
            input_tokens=row["input_tokens"] or 0,
            output_tokens=row["output_tokens"] or 0,
            message_count=row["message_count"],
            working_seconds=row["working_seconds"] or 0,
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


async def get_project_billing(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    from_date: str,
    to_date: str,
    visible_projects: set[str] | None = None,
) -> ProjectBillingSummary:
    """Billing summary from ``task_cost_entries`` for a project over the window.

    Visibility: same DECISION 1 enforcement as ``get_project_costs``. The billing
    config is read via ``cost_service._get_billing_config`` (resolved at call time
    so the existing patch seam is unaffected).
    """
    _enforce_project_access(slug, visible_projects)

    cursor = await db.execute(
        """SELECT
            COALESCE(SUM(tce.total_cost_usd), 0.0) AS total_cost,
            COALESCE(SUM(tce.total_bill_usd), 0.0) AS total_bill,
            COALESCE(SUM(CASE WHEN tce.entry_type='agent' THEN tce.total_cost_usd ELSE 0 END), 0.0) AS agent_cost,
            COALESCE(SUM(CASE WHEN tce.entry_type='human' THEN tce.total_cost_usd ELSE 0 END), 0.0) AS human_cost,
            COALESCE(SUM(CASE WHEN tce.is_billable=1 THEN tce.total_bill_usd ELSE 0 END), 0.0) AS billable,
            COALESCE(SUM(CASE WHEN tce.is_billable=0 THEN tce.total_cost_usd ELSE 0 END), 0.0) AS non_billable,
            COUNT(DISTINCT tce.task_id) AS task_count,
            COUNT(*) AS entry_count
           FROM task_cost_entries tce
           JOIN tasks t ON t.id = tce.task_id AND t.deleted_at IS NULL
           WHERE t.project = ?
           AND tce.created_at >= ? AND tce.created_at < date(?, '+1 day')""",
        (slug, from_date, to_date),
    )
    row = await cursor.fetchone()

    billing = cost_service._get_billing_config(slug)

    return ProjectBillingSummary(
        project_slug=slug,
        from_date=from_date,
        to_date=to_date,
        total_cost_usd=round(row["total_cost"] or 0.0, 4),
        total_bill_usd=round(row["total_bill"] or 0.0, 4),
        agent_cost_usd=round(row["agent_cost"] or 0.0, 4),
        human_cost_usd=round(row["human_cost"] or 0.0, 4),
        billable_usd=round(row["billable"] or 0.0, 4),
        non_billable_usd=round(row["non_billable"] or 0.0, 4),
        task_count=row["task_count"] or 0,
        entry_count=row["entry_count"] or 0,
        token_markup_factor=billing["token_markup_factor"],
        agent_bill_rate=billing["agent_bill_rate"],
        human_bill_rate=billing["human_bill_rate"],
    )
