"""Read-only, caller-scoped inputs for journal curation and synthesis advice."""
from __future__ import annotations

from typing import Any

from core.api.db import acquire_db
from core.api.services.access_grants import visible_projects_for_actor
from core.api.services.jev.journal_filter import filter_notable_events, should_resynthesize


def _event_scope(visible: set[str] | None) -> tuple[str, list[str]]:
    if visible is None:
        return "", []
    projects = sorted(visible)
    marks = ",".join("?" for _ in projects) or "NULL"
    return (f" AND (e.source_project IS NULL OR e.source_project = '' OR e.source_project IN ({marks}))"
            f" AND (e.target_project IS NULL OR e.target_project = '' OR e.target_project IN ({marks}))", projects * 2)


async def curate_journal(ctx: Any, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """One scoring batch over the sources of the already-visible journal page.

    The persisted journal and its sources remain intact. The curated event list
    and complete omission receipt are an additional, explicitly advisory view.
    """
    refs = list(dict.fromkeys(ref for entry in entries for ref in entry.get("body", {}).get("sources", [])))
    events = []
    async with acquire_db() as db:
        visible = await visible_projects_for_actor(db, ctx)
        clause, project_params = _event_scope(visible)
        for offset in range(0, len(refs), 400):
            batch = refs[offset:offset + 400]
            marks = ",".join("?" for _ in batch)
            rows = await (await db.execute(
                "SELECT e.event_id, e.title, e.summary FROM brain_digest_events e "
                "JOIN brain_runs r ON r.run_id=e.run_id WHERE r.workspace_id=? "
                f"AND e.event_id IN ({marks})" + clause + " ORDER BY e.observed_at DESC, e.event_id",
                [ctx.workspace_id, *batch, *project_params],
            )).fetchall()
            events.extend(dict(row) for row in rows)
    try:
        kept, receipt = await filter_notable_events(events)
        return {"events": kept, "receipt": receipt.to_dict(), "advisory": True}
    except Exception:
        return {"events": events, "receipt": {"surface": "journal_notability", "status": "error",
                "kept": len(events), "filtered": 0, "omitted": [], "unscored": len(events)}, "advisory": True}


async def synthesis_context(ctx: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Conservative watermark across caller-visible scopes, not global settings.

    Any scope without a prior narrative keeps the watermark unknown. The delta
    fetches one item beyond the scoring cap, making overflow explicit rather
    than declaring a quiet period from a sample.
    """
    from core.api.services.brain.run_selection import latest_readable_run

    async with acquire_db() as db:
        visible = await visible_projects_for_actor(db, ctx)
        clause, project_params = _event_scope(visible)
        sql = "SELECT scope_type, scope_key, MAX(narrative_agent_at) AS synthesized FROM brain_journal_entries WHERE workspace_id=?"
        params: list[Any] = [ctx.workspace_id]
        if visible is not None:
            projects = sorted(visible)
            marks = ",".join("?" for _ in projects) or "NULL"
            scope_filter = f"(scope_type='project' AND scope_key IN ({marks}))"
            if ctx.system_role in {"admin", "super_admin"}:
                scope_filter += " OR scope_type IN ('company','program')"
            sql += f" AND ({scope_filter})"
            params.extend(projects)
        rows = await (await db.execute(sql + " GROUP BY scope_type, scope_key", params)).fetchall()
        timestamps = [row["synthesized"] for row in rows]
        last = min(timestamps) if timestamps and all(timestamps) else None
        run = await latest_readable_run(db, workspace_id=ctx.workspace_id)
        delta = await (await db.execute(
            "SELECT e.event_id, e.title, e.summary FROM brain_digest_events e JOIN brain_runs r ON r.run_id=e.run_id "
            "WHERE r.workspace_id=? AND r.status IN ('succeeded','partial') "
            "AND r.superseded_by_run_id IS NULL "
            "AND (? IS NULL OR julianday(e.created_at)>julianday(?) "
            "OR julianday(r.updated_at)>julianday(?))" + clause +
            " ORDER BY e.observed_at, e.event_id LIMIT 41",
            [ctx.workspace_id, last, last, last, *project_params],
        )).fetchall()
    return {"last_synthesis_at": last, "last_cycle_key": run["cycle_key"] if run else None}, [dict(row) for row in delta]


async def synthesis_advice(ctx: Any) -> dict[str, Any]:
    metadata, events = await synthesis_context(ctx)
    try:
        advice = await should_resynthesize(**metadata, events_since_synthesis=events)
    except Exception:
        advice = {"should": True, "reasoning": "advisory check unavailable", "outcome": "error"}
    return {**metadata, "synthesis_advisory": {**advice, "advisory": True}}
