# Brain v1 — deterministic narrative recap (no LLM).
# Builds an italian-friendly "story of the cycle" by aggregating the same
# journal/digest tables the UI already reads, plus a small per-event title
# lookup so the founder sees real titles instead of BLAKE2b event_ids.
#
# v1.1 LLM polish layer (Gemma 3 12B QAT) sits on top of this — once the
# operator flips brain_llm_polish_enabled the polished narrative replaces
# the deterministic body. This module is the always-on baseline.
from __future__ import annotations

import json
from typing import Any

import aiosqlite

from core.api.db import acquire_db


DOMAIN_LABEL_IT: dict[str, str] = {
    "task": "task aggiornati",
    "pr": "pull request",
    "commit": "commit",
    "handoff": "handoff",
    "learning": "learning",
    "doc": "documenti aggiornati",
    "ingest": "ingest item",
    "kg": "nodi KG toccati",
    "external": "update esterni",
    "regression": "regressioni",
    "file": "file toccati",
}

SOURCE_LABEL_IT: dict[str, str] = {
    "pir": "Marvis",
    "git": "git",
    "kg": "knowledge graph",
    "ingest": "ingest",
    "handoff": "handoff",
    "learning": "learning",
    "ci": "CI",
    "docs_governance": "docs governance",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_latest_cycle_key(
    db: aiosqlite.Connection, *, workspace_id: str
) -> str | None:
    async with db.execute(
        "SELECT cycle_key FROM brain_runs "
        "WHERE workspace_id = ? AND status = 'succeeded' "
        "AND superseded_by_run_id IS NULL "
        "ORDER BY cycle_key DESC LIMIT 1",
        (workspace_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return row[0] if not hasattr(row, "keys") else row["cycle_key"]


async def _journal_entries(
    db: aiosqlite.Connection, *, cycle_key: str, workspace_id: str
) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT j.scope_type, j.scope_key, j.body_json, j.is_empty, j.run_id "
        "FROM brain_journal_entries j "
        "JOIN brain_runs r ON r.run_id = j.run_id "
        "WHERE j.workspace_id = ? AND j.cycle_key = ? "
        "AND r.status IN ('succeeded','partial') "
        "AND r.superseded_by_run_id IS NULL",
        (workspace_id, cycle_key),
    ) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        body_raw = r["body_json"] if hasattr(r, "keys") else r[2]
        try:
            body = json.loads(body_raw or "{}")
        except json.JSONDecodeError:
            body = {}
        out.append(
            {
                "scope_type": r["scope_type"] if hasattr(r, "keys") else r[0],
                "scope_key": r["scope_key"] if hasattr(r, "keys") else r[1],
                "body": body,
                "is_empty": bool(r["is_empty"] if hasattr(r, "keys") else r[3]),
                "run_id": r["run_id"] if hasattr(r, "keys") else r[4],
            }
        )
    return out


async def _events_by_id(
    db: aiosqlite.Connection,
    *,
    event_ids: list[str],
    cycle_key: str,
) -> dict[str, dict[str, Any]]:
    if not event_ids:
        return {}
    # Cap lookup size — title preview, not exhaustive fetch.
    unique_ids = list(dict.fromkeys(event_ids))[:200]
    placeholders = ",".join("?" for _ in unique_ids)
    query = (
        "SELECT event_id, event_type, source_system, source_project, title "
        f"FROM brain_digest_events WHERE cycle_key = ? AND event_id IN ({placeholders})"
    )
    params = [cycle_key, *unique_ids]
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        eid = r["event_id"] if hasattr(r, "keys") else r[0]
        out[eid] = {
            "event_id": eid,
            "event_type": r["event_type"] if hasattr(r, "keys") else r[1],
            "source_system": r["source_system"] if hasattr(r, "keys") else r[2],
            "source_project": r["source_project"] if hasattr(r, "keys") else r[3],
            "title": (r["title"] if hasattr(r, "keys") else r[4]) or "(senza titolo)",
        }
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _what_changed_summary(body: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in body.get("what_changed", []) or []:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain")
        ids = item.get("event_ids") or []
        if isinstance(domain, str):
            counts[domain] = counts.get(domain, 0) + (
                len(ids) if isinstance(ids, list) else 0
            )
    return counts


def _format_counts_it(counts: dict[str, int]) -> list[str]:
    parts: list[str] = []
    for domain, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n <= 0:
            continue
        label = DOMAIN_LABEL_IT.get(domain, domain)
        parts.append(f"{n} {label}")
    return parts


def _scope_narrative(scope_key: str, counts: dict[str, int], decisions: int) -> str:
    pieces = _format_counts_it(counts)
    if not pieces and decisions == 0:
        return f"Su {scope_key}: nessuna attività nel ciclo."
    tail_chunks: list[str] = []
    if pieces:
        if len(pieces) == 1:
            tail_chunks.append(pieces[0])
        elif len(pieces) == 2:
            tail_chunks.append(f"{pieces[0]} e {pieces[1]}")
        else:
            tail_chunks.append(", ".join(pieces[:-1]) + f", {pieces[-1]}")
    if decisions > 0:
        noun = "decisione osservata" if decisions == 1 else "decisioni osservate"
        tail_chunks.append(f"{decisions} {noun}")
    return f"Su {scope_key}: " + ", ".join(tail_chunks) + "."


def _company_narrative(counts: dict[str, int], decisions: int, projects_active: int) -> str:
    pieces = _format_counts_it(counts)
    if not pieces and decisions == 0:
        return "Nessuna attività significativa nel ciclo."
    chunks: list[str] = []
    if pieces:
        chunks.append(", ".join(pieces))
    if decisions > 0:
        noun = "decisione" if decisions == 1 else "decisioni"
        chunks.append(f"{decisions} {noun} osservate")
    if projects_active > 0:
        noun = "progetto" if projects_active == 1 else "progetti"
        chunks.append(f"{projects_active} {noun} attivi")
    return "Oggi: " + " · ".join(chunks) + "."


async def build_cycle_recap(
    *, cycle_key: str, workspace_id: str = "ws_default"
) -> dict[str, Any]:
    """Return a deterministic Italian narrative for a cycle.

    Shape:
      {
        cycle_key,
        resolved_cycle_key,
        company: { narrative, breakdown:{domain:int}, decisions_count },
        projects: [{ scope_key, narrative, breakdown, decisions:[{...}],
                     decisions_count }]
      }
    """
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        resolved = cycle_key
        if cycle_key == "latest":
            resolved = await _resolve_latest_cycle_key(db, workspace_id=workspace_id)
        if not resolved:
            return {
                "cycle_key": cycle_key,
                "resolved_cycle_key": None,
                "company": {
                    "narrative": "Nessun ciclo pubblicato.",
                    "breakdown": {},
                    "decisions_count": 0,
                },
                "projects": [],
            }
        entries = await _journal_entries(db, cycle_key=resolved, workspace_id=workspace_id)

        # Collect all decision event_ids across scopes — single batch lookup.
        all_decision_ids: list[str] = []
        for entry in entries:
            for ev_id in entry["body"].get("decisions_observed", []) or []:
                if isinstance(ev_id, str):
                    all_decision_ids.append(ev_id)
        event_titles = await _events_by_id(
            db, event_ids=all_decision_ids, cycle_key=resolved
        )

    company_breakdown: dict[str, int] = {}
    company_decisions = 0
    project_recaps: list[dict[str, Any]] = []
    project_scopes_with_activity = 0

    for entry in entries:
        if entry["is_empty"]:
            continue
        scope_type = entry["scope_type"]
        scope_key = entry["scope_key"]
        body = entry["body"]
        counts = _what_changed_summary(body)
        decisions_ids = [
            d for d in (body.get("decisions_observed") or []) if isinstance(d, str)
        ]
        decisions_count = len(decisions_ids)

        if scope_type == "company":
            company_breakdown = counts
            company_decisions = decisions_count
            continue

        if scope_type == "project":
            if counts or decisions_count:
                project_scopes_with_activity += 1
            top_decisions = []
            for ev_id in decisions_ids[:5]:
                ev_meta = event_titles.get(ev_id)
                if ev_meta is None:
                    top_decisions.append(
                        {
                            "event_id": ev_id,
                            "title": "(evento non più disponibile)",
                            "event_type": None,
                            "source_system": None,
                            "source_project": scope_key,
                        }
                    )
                else:
                    top_decisions.append(ev_meta)
            project_recaps.append(
                {
                    "scope_key": scope_key,
                    "narrative": _scope_narrative(scope_key, counts, decisions_count),
                    "breakdown": counts,
                    "decisions_count": decisions_count,
                    "decisions": top_decisions,
                }
            )

    # Stable sort: most active projects first.
    project_recaps.sort(
        key=lambda p: (
            -sum(p["breakdown"].values()),
            -p["decisions_count"],
            p["scope_key"],
        )
    )

    return {
        "cycle_key": cycle_key,
        "resolved_cycle_key": resolved,
        "company": {
            "narrative": _company_narrative(
                company_breakdown, company_decisions, project_scopes_with_activity
            ),
            "breakdown": company_breakdown,
            "decisions_count": company_decisions,
        },
        "projects": project_recaps,
    }


__all__ = ["build_cycle_recap"]
