# Brain v1 — CycleSnapshot loader (sub-02 C0).
# Single-pass projection of L2 (digest events + journal entries) for the seven
# drift rules. Rules MUST consume the snapshot — direct SELECT on
# brain_digest_events / brain_journal_entries is forbidden (layering invariant).
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from core.api.db import acquire_db

logger = logging.getLogger(__name__)


# Compile-once: DR5 procedure-change regex shared with the rule.
PROCEDURE_RE = re.compile(
    r"\b(?:procedura cambiata|nuovo processo|d'ora in poi|da oggi in poi|"
    r"nuova procedura|new process|from now on|procedure changed|updated workflow)\b",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(slots=True, frozen=True)
class DigestEventRow:
    """In-memory digest event projection (per-cycle snapshot)."""

    event_id: str
    event_type: str
    source_system: str
    source_project: str | None
    target_project: str | None
    program_key: str | None
    source_ref: str
    title: str
    summary: str
    observed_at: datetime
    evidence: dict[str, Any]


@dataclass(slots=True, frozen=True)
class JournalEntryRow:
    """In-memory journal entry projection (per-cycle snapshot)."""

    entry_id: str
    cycle_key: str
    scope_type: str
    scope_key: str
    program_key: str | None
    body: dict[str, Any]
    is_empty: bool
    published_at: datetime


@dataclass(slots=True, frozen=True)
class CycleSnapshot:
    """Read-only L2 projection consumed by all DR rules.

    Rules MUST NOT issue raw SQL — every read goes through this snapshot.
    """

    cycle_key: str
    run_id: str
    as_of: datetime
    workspace_id: str
    lookback_cycles: int

    events: tuple[DigestEventRow, ...]
    by_event_type: dict[str, list[DigestEventRow]]
    by_scope: dict[tuple[str, str], list[DigestEventRow]]
    by_source_ref: dict[str, DigestEventRow]
    decision_marker_events: tuple[DigestEventRow, ...]
    procedure_keyword_hits: tuple[DigestEventRow, ...]
    external_update_events: tuple[DigestEventRow, ...]

    journal_entries: dict[tuple[str, str], JournalEntryRow]
    prior_journal_entries: tuple[JournalEntryRow, ...]

    project_program: dict[str, str | None] = field(default_factory=dict)


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_evidence(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def _fetch_events(
    db: aiosqlite.Connection, *, run_id: str
) -> list[DigestEventRow]:
    rows = await (
        await db.execute(
            "SELECT event_id, event_type, source_system, source_project, "
            "       target_project, program_key, source_ref, title, summary, "
            "       observed_at, evidence_json "
            "FROM brain_digest_events WHERE run_id = ?",
            (run_id,),
        )
    ).fetchall()
    events: list[DigestEventRow] = []
    for r in rows:
        get = (lambda key: r[key]) if hasattr(r, "keys") else None
        if get is None:
            (
                event_id,
                event_type,
                source_system,
                source_project,
                target_project,
                program_key,
                source_ref,
                title,
                summary,
                observed_at,
                evidence_json,
            ) = r
        else:
            event_id = get("event_id")
            event_type = get("event_type")
            source_system = get("source_system")
            source_project = get("source_project")
            target_project = get("target_project")
            program_key = get("program_key")
            source_ref = get("source_ref")
            title = get("title")
            summary = get("summary")
            observed_at = get("observed_at")
            evidence_json = get("evidence_json")
        events.append(
            DigestEventRow(
                event_id=event_id,
                event_type=event_type,
                source_system=source_system,
                source_project=source_project,
                target_project=target_project,
                program_key=program_key,
                source_ref=source_ref,
                title=title,
                summary=summary or "",
                observed_at=_parse_iso(observed_at),
                evidence=_parse_evidence(evidence_json),
            )
        )
    return events


async def _fetch_journal_for_run(
    db: aiosqlite.Connection, *, run_id: str
) -> list[JournalEntryRow]:
    rows = await (
        await db.execute(
            "SELECT entry_id, cycle_key, scope_type, scope_key, program_key, "
            "       body_json, is_empty, published_at "
            "FROM brain_journal_entries WHERE run_id = ?",
            (run_id,),
        )
    ).fetchall()
    return [_journal_row(r) for r in rows]


async def _fetch_prior_journal(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    cycle_key: str,
    lookback_cycles: int,
) -> list[JournalEntryRow]:
    if lookback_cycles <= 0:
        return []
    try:
        ck_date = datetime.fromisoformat(cycle_key).date()
    except ValueError:
        return []
    since = (ck_date - timedelta(days=lookback_cycles)).isoformat()
    rows = await (
        await db.execute(
            "SELECT j.entry_id, j.cycle_key, j.scope_type, j.scope_key, "
            "       j.program_key, j.body_json, j.is_empty, j.published_at "
            "FROM brain_journal_entries j "
            "JOIN brain_runs r ON r.run_id = j.run_id "
            "WHERE j.workspace_id = ? AND j.cycle_key >= ? AND j.cycle_key < ? "
            "AND r.status IN ('succeeded','partial') "
            "AND r.superseded_by_run_id IS NULL "
            "ORDER BY j.cycle_key DESC",
            (workspace_id, since, cycle_key),
        )
    ).fetchall()
    return [_journal_row(r) for r in rows]


def _journal_row(r: Any) -> JournalEntryRow:
    if hasattr(r, "keys"):
        body_raw = r["body_json"]
        return JournalEntryRow(
            entry_id=r["entry_id"],
            cycle_key=r["cycle_key"],
            scope_type=r["scope_type"],
            scope_key=r["scope_key"],
            program_key=r["program_key"],
            body=_parse_body(body_raw),
            is_empty=bool(r["is_empty"]),
            published_at=_parse_iso(r["published_at"]),
        )
    (
        entry_id,
        cycle_key,
        scope_type,
        scope_key,
        program_key,
        body_raw,
        is_empty,
        published_at,
    ) = r
    return JournalEntryRow(
        entry_id=entry_id,
        cycle_key=cycle_key,
        scope_type=scope_type,
        scope_key=scope_key,
        program_key=program_key,
        body=_parse_body(body_raw),
        is_empty=bool(is_empty),
        published_at=_parse_iso(published_at),
    )


def _parse_body(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _index_events(
    events: list[DigestEventRow],
) -> tuple[
    dict[str, list[DigestEventRow]],
    dict[tuple[str, str], list[DigestEventRow]],
    dict[str, DigestEventRow],
    list[DigestEventRow],
    list[DigestEventRow],
    list[DigestEventRow],
    dict[str, str | None],
]:
    by_event_type: dict[str, list[DigestEventRow]] = defaultdict(list)
    by_scope: dict[tuple[str, str], list[DigestEventRow]] = defaultdict(list)
    by_source_ref: dict[str, DigestEventRow] = {}
    decision_marker_events: list[DigestEventRow] = []
    procedure_keyword_hits: list[DigestEventRow] = []
    external_update_events: list[DigestEventRow] = []
    project_program: dict[str, str | None] = {}

    for ev in events:
        by_event_type[ev.event_type].append(ev)
        by_source_ref.setdefault(ev.source_ref, ev)

        scopes: set[tuple[str, str]] = set()
        for slug in (ev.source_project, ev.target_project):
            if slug:
                scopes.add(("project", slug))
                project_program.setdefault(slug, ev.program_key)
        if ev.program_key:
            scopes.add(("program", ev.program_key))
        scopes.add(("company", "__company__"))
        for key in scopes:
            by_scope[key].append(ev)

        marker = ev.evidence.get("decision_marker")
        if isinstance(marker, str) and marker:
            decision_marker_events.append(ev)

        if ev.summary and PROCEDURE_RE.search(ev.summary):
            procedure_keyword_hits.append(ev)

        if ev.event_type == "external_update_seen":
            external_update_events.append(ev)

    return (
        dict(by_event_type),
        dict(by_scope),
        by_source_ref,
        decision_marker_events,
        procedure_keyword_hits,
        external_update_events,
        project_program,
    )


async def build_snapshot(
    cycle_key: str,
    *,
    run_id: str,
    workspace_id: str = "ws_default",
    lookback_cycles: int = 7,
    as_of: datetime | None = None,
) -> CycleSnapshot:
    """Build a single-pass projection of L2 outputs for the given run.

    Pure read-only — never writes to substrate or sibling Brain tables.
    """
    now = as_of or datetime.now(timezone.utc)
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        events = await _fetch_events(db, run_id=run_id)
        current_journal = await _fetch_journal_for_run(db, run_id=run_id)
        prior_journal = await _fetch_prior_journal(
            db,
            workspace_id=workspace_id,
            cycle_key=cycle_key,
            lookback_cycles=lookback_cycles,
        )

    (
        by_event_type,
        by_scope,
        by_source_ref,
        decision_marker_events,
        procedure_keyword_hits,
        external_update_events,
        project_program,
    ) = _index_events(events)

    journal_entries: dict[tuple[str, str], JournalEntryRow] = {
        (e.scope_type, e.scope_key): e for e in current_journal
    }

    return CycleSnapshot(
        cycle_key=cycle_key,
        run_id=run_id,
        as_of=now,
        workspace_id=workspace_id,
        lookback_cycles=lookback_cycles,
        events=tuple(events),
        by_event_type=by_event_type,
        by_scope=by_scope,
        by_source_ref=by_source_ref,
        decision_marker_events=tuple(decision_marker_events),
        procedure_keyword_hits=tuple(procedure_keyword_hits),
        external_update_events=tuple(external_update_events),
        journal_entries=journal_entries,
        prior_journal_entries=tuple(prior_journal),
        project_program=project_program,
    )


__all__ = [
    "CycleSnapshot",
    "DigestEventRow",
    "JournalEntryRow",
    "PROCEDURE_RE",
    "build_snapshot",
]
