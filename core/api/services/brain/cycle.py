# Brain v1 cycle math + persistence (sub-01 D3 + portions of D1/D2).
# Cycle math is owned by Brain — NEVER import from inbox_digest_jobs (parent §10 anti-pattern).
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Any

import aiosqlite

from core.api.services.brain.models import EventDraft, SourceFailure
from core.api.services.brain.scope import resolve_program

logger = logging.getLogger(__name__)


_DECISION_MARKERS: frozenset[str] = frozenset(
    {
        "merged",
        "deployed",
        "approved",
        "rejected",
        "closed_won",
        "closed_lost",
        "created_with_severity_critical",
        "decision",
    }
)

_DECISION_EVENT_TYPES: frozenset[str] = frozenset(
    {"pr_changed", "task_changed", "learning_changed", "handoff_changed"}
)


# ----------------------------------------------------------------------
# Cycle math (own this — do not import from inbox_digest_jobs)
# ----------------------------------------------------------------------


def current_brain_cycle_key(now: datetime, freeze_hour_utc: int) -> str:
    """Return YYYY-MM-DD (UTC) for the current cycle.

    If wall clock is before freeze_hour_utc, the cycle still belongs to the
    previous day — same rule as inbox_digest_jobs._current_cycle_key. We copy
    the formula rather than import (cross-domain coupling guard).
    """
    if now.hour < freeze_hour_utc:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def cycle_cutoff_at(now: datetime, cutoff_hour_utc: int) -> datetime:
    """UTC ISO timestamp marking the substrate cutoff for this cycle."""
    cycle_date = now.date()
    if now.hour < cutoff_hour_utc:
        cycle_date = cycle_date - timedelta(days=1)
    return datetime.combine(
        cycle_date,
        time(hour=cutoff_hour_utc, tzinfo=timezone.utc),
    )


# ----------------------------------------------------------------------
# Stable event id derivation (BLAKE2b 16-byte, hex 32 chars)
# ----------------------------------------------------------------------


def canonical_evidence(evidence: dict[str, Any]) -> str:
    """Canonical JSON for hash stability. sort_keys + no whitespace."""
    return json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def evidence_hash(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_evidence(evidence).encode("utf-8")).hexdigest()


def make_event_id(
    *, cycle_key: str, event_type: str, source_ref: str, evidence_hash_hex: str
) -> str:
    """Deterministic BLAKE2b stable id, 16 bytes → 32 hex chars."""
    payload = f"{cycle_key}|{event_type}|{source_ref}|{evidence_hash_hex}".encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


# ----------------------------------------------------------------------
# brain_runs CRUD
# ----------------------------------------------------------------------


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def insert_brain_run(
    db: aiosqlite.Connection,
    *,
    cycle_key: str,
    workspace_id: str,
    cycle_window_start_utc: datetime,
    cycle_window_end_utc: datetime,
    cutoff_hour_utc_at_run: int,
    trigger: str,
    triggered_by: str | None,
    now: datetime,
) -> str:
    """Insert a brain_runs row with status='running' and return run_id."""
    run_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO brain_runs ("
        " run_id, workspace_id, cycle_key,"
        " cycle_window_start_utc, cycle_window_end_utc, cutoff_hour_utc_at_run,"
        " scope_type, scope_key, trigger, triggered_by, started_at, status"
        ") VALUES (?, ?, ?, ?, ?, ?, 'company', '__company__', ?, ?, ?, 'running')",
        (
            run_id,
            workspace_id,
            cycle_key,
            _utc_iso(cycle_window_start_utc),
            _utc_iso(cycle_window_end_utc),
            cutoff_hour_utc_at_run,
            trigger,
            triggered_by,
            _utc_iso(now),
        ),
    )
    return run_id


async def update_run_status(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    status: str,
    event_count: int = 0,
    partial_failures: list[SourceFailure] | None = None,
    duration_ms: int | None = None,
    error_summary: str | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Update terminal state on brain_runs."""
    failures_json = json.dumps(
        [
            {"source_system": f.source_system, "error": f.error, "traceback": f.traceback}
            for f in (partial_failures or [])
        ]
    )
    await db.execute(
        "UPDATE brain_runs SET "
        " status = ?,"
        " event_count = ?,"
        " partial_failures_json = ?,"
        " duration_ms = ?,"
        " error_summary = ?,"
        " finished_at = ?"
        " WHERE run_id = ?",
        (
            status,
            event_count,
            failures_json,
            duration_ms,
            error_summary,
            _utc_iso(finished_at) if finished_at else None,
            run_id,
        ),
    )


async def supersede_active_runs(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    cycle_key: str,
    new_run_id: str,
) -> None:
    """Mark prior active runs for the same cycle as superseded.

    Must be called BEFORE inserting the new run, otherwise the partial unique
    index uniq_brain_runs_active_cycle will fire.

    Wave 3.1 fix (Emilio 2026-05-19): `partial` runs are also superseded —
    senza questo i journal entries di un run partial restavano "canonical"
    nel reader filter (`r.status IN ('succeeded', 'partial') AND
    superseded_by_run_id IS NULL`), affiancando l'entry empty del run
    succeeded successivo e confondendo la UI.
    """
    await db.execute(
        "UPDATE brain_runs SET status = 'superseded', superseded_by_run_id = ? "
        "WHERE workspace_id = ? AND cycle_key = ? "
        "AND status IN ('running', 'succeeded', 'partial') "
        "AND superseded_by_run_id IS NULL",
        (new_run_id, workspace_id, cycle_key),
    )


async def supersede_orphan_partials(
    db: aiosqlite.Connection,
    *,
    workspace_id: str = "ws_default",
) -> int:
    """One-shot backfill: mark partial runs orphan (no `superseded_by_run_id`)
    as superseded by the latest succeeded run for the same `(workspace_id,
    cycle_key)`.

    Wave 3.1 (Emilio 2026-05-19): used to clean up historical partial runs
    that landed in DB before the supersede_active_runs fix included
    `'partial'` in its WHERE clause. Idempotent — partials without a later
    succeeded run stay untouched.

    Returns the count of rows updated.
    """
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT p.run_id AS partial_id,"
        "       (SELECT s.run_id FROM brain_runs s"
        "        WHERE s.workspace_id = p.workspace_id"
        "          AND s.cycle_key = p.cycle_key"
        "          AND s.status = 'succeeded'"
        "          AND s.superseded_by_run_id IS NULL"
        "          AND s.started_at > p.started_at"
        "        ORDER BY s.started_at DESC LIMIT 1) AS succ_id"
        "  FROM brain_runs p"
        " WHERE p.workspace_id = ?"
        "   AND p.status = 'partial'"
        "   AND p.superseded_by_run_id IS NULL",
        (workspace_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    pairs = [(r["partial_id"], r["succ_id"]) for r in rows if r["succ_id"]]
    if not pairs:
        return 0
    updated = 0
    for partial_id, succ_id in pairs:
        result = await db.execute(
            "UPDATE brain_runs SET status = 'superseded',"
            "  superseded_by_run_id = ?"
            " WHERE run_id = ?"
            "   AND status = 'partial'"
            "   AND superseded_by_run_id IS NULL",
            (succ_id, partial_id),
        )
        updated += result.rowcount if result.rowcount is not None else 0
    return updated


# ----------------------------------------------------------------------
# Event persistence
# ----------------------------------------------------------------------


def derive_event_id(
    *, cycle_key: str, event_type: str, source_ref: str, evidence: dict[str, Any]
) -> tuple[str, str]:
    """Return (event_id, evidence_hash) for the given canonicalized evidence."""
    ev_hash = evidence_hash(evidence)
    return (
        make_event_id(
            cycle_key=cycle_key,
            event_type=event_type,
            source_ref=source_ref,
            evidence_hash_hex=ev_hash,
        ),
        ev_hash,
    )


async def persist_event(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    cycle_key: str,
    draft: EventDraft,
) -> str:
    """Insert one event with deterministic id + INSERT OR IGNORE idempotency."""
    evidence = dict(draft.evidence or {})
    # Reject non-JSON-serializable evidence eagerly (test scenario).
    canonical_evidence(evidence)
    ev_hash = evidence_hash(evidence)
    event_id = make_event_id(
        cycle_key=cycle_key,
        event_type=draft.event_type,
        source_ref=draft.source_ref,
        evidence_hash_hex=ev_hash,
    )
    program_key = draft.program_key or resolve_program(
        draft.source_project or draft.target_project
    )
    await db.execute(
        "INSERT OR IGNORE INTO brain_digest_events ("
        " event_id, run_id, cycle_key,"
        " observed_at, derived_from_state_at,"
        " event_type, schema_version, source_system,"
        " source_project, target_project, program_key,"
        " source_ref, title, summary, evidence_json, evidence_hash"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            run_id,
            cycle_key,
            _utc_iso(draft.observed_at),
            _utc_iso(draft.derived_from_state_at),
            draft.event_type,
            draft.schema_version,
            draft.source_system,
            draft.source_project,
            draft.target_project,
            program_key,
            draft.source_ref,
            draft.title,
            draft.summary,
            json.dumps(evidence, sort_keys=True, ensure_ascii=False),
            ev_hash,
        ),
    )
    return event_id


# ----------------------------------------------------------------------
# Journal aggregation
# ----------------------------------------------------------------------


def _classify_decision(event_type: str, evidence: dict[str, Any]) -> bool:
    if event_type not in _DECISION_EVENT_TYPES:
        return False
    marker = evidence.get("decision_marker")
    return isinstance(marker, str) and marker in _DECISION_MARKERS


def _build_body_for_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    what_changed: list[dict[str, Any]] = []
    decisions: list[str] = []
    sources: list[str] = []
    notable: list[dict[str, Any]] = []
    domain_groups: dict[str, list[str]] = defaultdict(list)

    for ev in events:
        ev_id = ev["event_id"]
        sources.append(ev_id)
        domain_key = ev.get("event_type", "unknown").split("_", 1)[0]
        domain_groups[domain_key].append(ev_id)
        try:
            evidence_obj = json.loads(ev.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence_obj = {}
        if _classify_decision(ev["event_type"], evidence_obj):
            decisions.append(ev_id)
        salience = evidence_obj.get("salience")
        if isinstance(salience, (int, float)) and salience >= 0.7:
            notable.append({"event_id": ev_id, "title": ev.get("title", "")})

    for domain, ids in sorted(domain_groups.items()):
        what_changed.append({"domain": domain, "event_ids": ids})

    return {
        "what_changed": what_changed,
        "decisions_observed": decisions,
        "open_loops": [],
        "notable_context": notable,
        "sources": sources,
        "tomorrow_watch": [],
    }


def _empty_body() -> dict[str, Any]:
    return {
        "what_changed": [],
        "decisions_observed": [],
        "open_loops": [],
        "notable_context": [],
        "sources": [],
        "tomorrow_watch": [],
    }


def _hydrate_event_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Internal: row → dict mapping shared by run-scoped and cycle-scoped reads."""
    return [
        {
            "event_id": r[0] if not hasattr(r, "keys") else r["event_id"],
            "event_type": r[1] if not hasattr(r, "keys") else r["event_type"],
            "source_project": r[2] if not hasattr(r, "keys") else r["source_project"],
            "target_project": r[3] if not hasattr(r, "keys") else r["target_project"],
            "program_key": r[4] if not hasattr(r, "keys") else r["program_key"],
            "title": r[5] if not hasattr(r, "keys") else r["title"],
            "evidence_json": r[6] if not hasattr(r, "keys") else r["evidence_json"],
        }
        for r in rows
    ]


async def fetch_events_for_run(
    db: aiosqlite.Connection, *, run_id: str
) -> list[dict[str, Any]]:
    """Read all events for a run. Caller decides on memory bounds."""
    rows = await (
        await db.execute(
            "SELECT event_id, event_type, source_project, target_project, program_key, "
            "       title, evidence_json "
            "FROM brain_digest_events WHERE run_id = ?",
            (run_id,),
        )
    ).fetchall()
    return _hydrate_event_rows(rows)


async def fetch_events_for_cycle(
    db: aiosqlite.Connection,
    *,
    cycle_key: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Read events for a `(cycle_key, workspace_id)` deduped by event_id.

    Wave 3.1 fix (Emilio 2026-05-19): `persist_event` ha `INSERT OR IGNORE`
    su event_id deterministico — quando un recompute force=true ri-collecta
    gli stessi events, le INSERT duplicate vengono droppate e il nuovo run
    vede 0 events nella sua `SELECT WHERE run_id`. Risultato: la canonical
    succeeded run di un cycle ricomputato emetteva solo `is_empty=1` company
    entry.

    Soluzione: aggregare cross-run per cycle_key (event_id è già unique-key
    deterministico, quindi nessun dedup esplicito necessario), così
    `publish_run_journals` può popolare il body anche quando gli events
    raw appartengono fisicamente a precedenti run superseded.
    """
    # brain_digest_events ha cycle_key ma non workspace_id — uso JOIN
    # su brain_runs per filtrare. Eseguo DISTINCT su event_id per dedupare
    # se la stessa evidence è arrivata sotto run_id diversi nel cycle
    # (raro ma possibile con recompute force=true + INSERT OR IGNORE).
    rows = await (
        await db.execute(
            "SELECT DISTINCT e.event_id, e.event_type, e.source_project,"
            "       e.target_project, e.program_key, e.title, e.evidence_json"
            "  FROM brain_digest_events e"
            "  JOIN brain_runs r ON r.run_id = e.run_id"
            " WHERE e.cycle_key = ? AND r.workspace_id = ?",
            (cycle_key, workspace_id),
        )
    ).fetchall()
    return _hydrate_event_rows(rows)


async def publish_run_journals(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    cycle_key: str,
    workspace_id: str,
    now: datetime,
    max_events: int = 10_000,
) -> int:
    """Aggregate events for a run into company/program/project journal entries.

    Returns count of entries written (always >= 1 because empty cycles still
    emit an `is_empty=1` company entry).

    Wave 3.1 (Emilio 2026-05-19): legge events cycle-wide invece di filtrare
    solo per run_id, perché `persist_event INSERT OR IGNORE` deduplica gli
    event_id deterministici tra run successive del cycle. Senza questo, un
    recompute force=true emetteva sempre company empty (events già nel DB
    sotto run precedenti, nessuna nuova INSERT visibile a questo run).
    """
    events = await fetch_events_for_cycle(
        db, cycle_key=cycle_key, workspace_id=workspace_id
    )
    if len(events) > max_events:
        raise RuntimeError(
            f"cycle_too_large: {len(events)} events > {max_events} cap"
        )

    company_body = _build_body_for_events(events)
    is_empty_company = 1 if not events else 0

    entries_written = 0

    await _upsert_journal_entry(
        db,
        run_id=run_id,
        workspace_id=workspace_id,
        cycle_key=cycle_key,
        scope_type="company",
        scope_key="__company__",
        program_key=None,
        body=company_body,
        is_empty=is_empty_company,
        now=now,
    )
    entries_written += 1

    project_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    program_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    project_program: dict[str, str | None] = {}

    for ev in events:
        scopes: set[str] = set()
        if ev.get("source_project"):
            scopes.add(ev["source_project"])
        if ev.get("target_project"):
            scopes.add(ev["target_project"])
        for slug in scopes:
            project_buckets[slug].append(ev)
            program = ev.get("program_key") or resolve_program(slug)
            project_program.setdefault(slug, program)
        program_key = ev.get("program_key")
        if program_key:
            program_buckets[program_key].append(ev)

    for slug, slug_events in project_buckets.items():
        await _upsert_journal_entry(
            db,
            run_id=run_id,
            workspace_id=workspace_id,
            cycle_key=cycle_key,
            scope_type="project",
            scope_key=slug,
            program_key=project_program.get(slug),
            body=_build_body_for_events(slug_events),
            is_empty=0,
            now=now,
        )
        entries_written += 1

    for prog_key, prog_events in program_buckets.items():
        await _upsert_journal_entry(
            db,
            run_id=run_id,
            workspace_id=workspace_id,
            cycle_key=cycle_key,
            scope_type="program",
            scope_key=prog_key,
            program_key=prog_key,
            body=_build_body_for_events(prog_events),
            is_empty=0,
            now=now,
        )
        entries_written += 1

    return entries_written


async def _upsert_journal_entry(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    workspace_id: str,
    cycle_key: str,
    scope_type: str,
    scope_key: str,
    program_key: str | None,
    body: dict[str, Any],
    is_empty: int,
    now: datetime,
) -> None:
    body_json = json.dumps(body, sort_keys=True, ensure_ascii=False)
    entry_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO brain_journal_entries ("
        " entry_id, run_id, workspace_id, cycle_key,"
        " scope_type, scope_key, program_key,"
        " body_json, is_empty, published_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, scope_type, scope_key) DO UPDATE SET "
        " body_json = excluded.body_json,"
        " is_empty = excluded.is_empty,"
        " program_key = excluded.program_key,"
        " published_at = excluded.published_at",
        (
            entry_id,
            run_id,
            workspace_id,
            cycle_key,
            scope_type,
            scope_key,
            program_key,
            body_json,
            is_empty,
            _utc_iso(now),
        ),
    )


# ----------------------------------------------------------------------
# Wave 3.1 gap 2 — persistent journal narrative polish at cycle time.
# ----------------------------------------------------------------------
#
# Unlike the read-time polish in llm/router_glue.py (TTL cache, transient),
# this helper writes narrative_polished + at + model directly to the row
# so historical reads (Console "Giornale ultimi 30 giorni") never miss a
# polish. Best-effort: failures keep the deterministic body_json visible.

_POLISH_KEEP_SECTIONS = ("what_changed", "decisions_observed", "open_loops", "notable_context")


def _polish_entries_for_prompt(body: dict[str, Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for label in _POLISH_KEEP_SECTIONS:
        items = body.get(label) or []
        if not items:
            continue
        flat.append({"section": label, "items": items})
    return flat


def _polish_allowed_refs(body: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for ref in body.get("sources") or []:
        if isinstance(ref, str) and ref:
            refs.append(ref)
    for label in _POLISH_KEEP_SECTIONS + ("tomorrow_watch",):
        for item in body.get(label) or []:
            if isinstance(item, dict):
                ref = item.get("ref") or item.get("evidence_ref")
                if isinstance(ref, str) and ref:
                    refs.append(ref)
    seen: set[str] = set()
    out: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _polish_deterministic_narrative(body: dict[str, Any]) -> str:
    decisions = body.get("decisions_observed") or []
    if decisions:
        return " ".join(str(d) for d in decisions[:3])
    open_loops = body.get("open_loops") or []
    if open_loops:
        first = open_loops[0]
        if isinstance(first, dict):
            title = first.get("title") or first.get("summary")
            if isinstance(title, str) and title:
                return title
    return ""


# ----------------------------------------------------------------------
# Wave 3.1 smart polish payload (Emilio 2026-05-19 19:48 — decisione regole).
# ----------------------------------------------------------------------
#
# Il body_json delle entries scope=company aggrega fino a 1100+ event_ids
# (es. 2026-05-19 company __company__ ha 1121 sources, 80KB di JSON). Il
# polish gateway tier-write rifiuta HTTP 422 quando il prompt supera il
# context window di Gemma 3 12B QAT.
#
# Regole concordate per ridurre il payload mantenendo significato:
#   - PR mergiati (`pr_changed` con marker decisional): tutti, titolo + project
#   - Task completati (`task_changed` con marker decisional): tutti
#   - Learning registrati (`learning_changed`): tutti
#   - Handoff scritti (`handoff_changed`): max 3 per progetto, recency DESC
#   - Commit (`commit_changed` / `file_changed`): aggregato per progetto, no list
#   - KG (`kg_changed`): top-5 nodi più toccati nel cycle + count totale
#   - Altri (`ingest_changed`, `doc_changed`, `regression_signal`,
#     `external_update_seen`): aggregato per event_type, no list
#
# Output target: ~3KB JSON invece di 80KB → Gemma context safe (~1.5K tokens).

_DECISION_MARKERS_LOWER = frozenset(m.lower() for m in _DECISION_MARKERS)
_HANDOFFS_PER_PROJECT_CAP = 2
_KG_HOTSPOTS_CAP = 5
_DECISIONS_TOTAL_CAP = 15   # Mac Gateway 4KB prompt budget gate (Gemma 12B prefill latency)
_ALLOWED_REFS_CAP = 30
_TITLE_MAX_CHARS = 90


def _is_decisional(event_type: str, evidence_json: str, title: str, summary: str) -> bool:
    """An event qualifies as 'decision' when it's a state-change marker
    or its evidence carries one of the canonical markers.

    Robust to bodies where evidence is sparse — we also scan title/summary.
    """
    if event_type not in _DECISION_EVENT_TYPES:
        return False
    haystack = " ".join((evidence_json or "", title or "", summary or "")).lower()
    return any(marker in haystack for marker in _DECISION_MARKERS_LOWER)


async def _fetch_run_events(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    scope_type: str,
    scope_key: str,
    cycle_key: str | None = None,
    workspace_id: str = "ws_default",
) -> list[dict[str, Any]]:
    """Read events for the smart polish payload.

    Wave 3.1 cycle-wide hop (Emilio 2026-05-19): se `cycle_key` è fornito,
    legge cross-run per `(cycle_key, workspace_id)` filtrato per scope.
    Necessario perché `persist_event INSERT OR IGNORE` deduplica event_id
    deterministici tra recompute successivi — il canonical run finisce con
    0 events sotto la sua run_id, e la SELECT WHERE run_id ritorna [] →
    polish skip `no_refs`.

    Senza `cycle_key` (fallback) usa il filter `run_id` legacy.

    company → tutti gli events
    project → events con source_project = scope_key OR target_project = scope_key
    program → events con program_key = scope_key
    """
    if cycle_key is not None:
        where = ["e.cycle_key = ?", "r.workspace_id = ?"]
        params: list[Any] = [cycle_key, workspace_id]
        if scope_type == "project":
            where.append("(e.source_project = ? OR e.target_project = ?)")
            params.extend([scope_key, scope_key])
        elif scope_type == "program":
            where.append("e.program_key = ?")
            params.append(scope_key)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT DISTINCT e.event_id, e.event_type, e.source_system,"
            " e.source_project, e.target_project, e.program_key, e.source_ref,"
            " e.title, e.summary, e.evidence_json, e.observed_at"
            "  FROM brain_digest_events e"
            "  JOIN brain_runs r ON r.run_id = e.run_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY e.observed_at DESC",
            params,
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    where = ["run_id = ?"]
    params = [run_id]
    if scope_type == "project":
        where.append("(source_project = ? OR target_project = ?)")
        params.extend([scope_key, scope_key])
    elif scope_type == "program":
        where.append("program_key = ?")
        params.append(scope_key)

    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT event_id, event_type, source_system, source_project, target_project,"
        " program_key, source_ref, title, summary, evidence_json, observed_at"
        " FROM brain_digest_events"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY observed_at DESC",
        params,
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


def _build_smart_polish_payload(
    *,
    events: list[dict[str, Any]],
    scope_type: str,
    scope_key: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply Emilio's reduction rules. Returns `(entries, allowed_refs)`.

    `entries` is a single-element list with `section='cycle_summary'` so
    `polish_journal_entry` keeps its current call shape. The payload inside
    is a structured dict with `decisions`, `handoffs`, `commits_by_project`,
    `kg_hotspots`, `other_aggregates`.
    """
    decisions: list[dict[str, Any]] = []
    handoffs_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    commits_by_project: dict[str, int] = defaultdict(int)
    kg_by_ref: dict[str, dict[str, Any]] = {}
    other_by_type: dict[str, int] = defaultdict(int)
    allowed_refs: list[str] = []

    for ev in events:
        et = ev.get("event_type") or ""
        title = ev.get("title") or ""
        summary = ev.get("summary") or ""
        evidence_json = ev.get("evidence_json") or ""
        proj = ev.get("source_project") or ev.get("target_project") or "unknown"
        source_ref = ev.get("source_ref") or ""

        # PR / task / learning / handoff
        if et == "pr_changed" and _is_decisional(et, evidence_json, title, summary):
            decisions.append({
                "type": "pr_merged",
                "project": proj,
                "title": title[:_TITLE_MAX_CHARS],
                "ref": source_ref,
            })
            allowed_refs.append(source_ref)
            continue

        if et == "task_changed" and _is_decisional(et, evidence_json, title, summary):
            decisions.append({
                "type": "task_completed",
                "project": proj,
                "title": title[:_TITLE_MAX_CHARS],
                "ref": source_ref,
            })
            allowed_refs.append(source_ref)
            continue

        if et == "learning_changed":
            decisions.append({
                "type": "learning_registered",
                "project": proj,
                "title": title[:_TITLE_MAX_CHARS],
                "ref": source_ref,
            })
            allowed_refs.append(source_ref)
            continue

        if et == "handoff_changed":
            bucket = handoffs_by_project[proj]
            if len(bucket) < _HANDOFFS_PER_PROJECT_CAP:
                bucket.append({
                    "project": proj,
                    "title": title[:_TITLE_MAX_CHARS],
                    "ref": source_ref,
                })
                allowed_refs.append(source_ref)
            continue

        if et in ("commit_changed", "file_changed"):
            commits_by_project[proj] += 1
            continue

        if et == "kg_changed":
            slot = kg_by_ref.get(source_ref)
            if slot is None:
                kg_by_ref[source_ref] = {
                    "ref": source_ref,
                    "project": proj,
                    "title": title[:160],
                    "count": 1,
                }
            else:
                slot["count"] += 1
            continue

        # Catch-all
        other_by_type[et] += 1

    # Flatten handoffs preserving project order (alphabetical for stability).
    handoffs: list[dict[str, Any]] = []
    for proj in sorted(handoffs_by_project.keys()):
        handoffs.extend(handoffs_by_project[proj])

    # Decisions cap — keep most recent (events arrive observed_at DESC).
    if len(decisions) > _DECISIONS_TOTAL_CAP:
        decisions = decisions[:_DECISIONS_TOTAL_CAP]

    # KG hotspots — top by touch count.
    kg_hotspots = sorted(
        kg_by_ref.values(), key=lambda h: (-int(h.get("count", 0)), h.get("ref", ""))
    )[:_KG_HOTSPOTS_CAP]
    for hot in kg_hotspots:
        ref = hot.get("ref")
        if isinstance(ref, str) and ref:
            allowed_refs.append(ref)

    kg_total = sum(int(h.get("count", 0)) for h in kg_by_ref.values())

    payload = {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "decisions": decisions,
        "handoffs": handoffs,
        "kg_hotspots": kg_hotspots,
        "aggregates": {
            "commits_by_project": dict(
                sorted(commits_by_project.items(), key=lambda kv: -kv[1])
            ),
            "kg_changes_total": kg_total,
            "other_events_by_type": dict(other_by_type),
            "events_total": len(events),
        },
    }

    seen: set[str] = set()
    dedup_refs: list[str] = []
    for ref in allowed_refs:
        if ref and ref not in seen:
            seen.add(ref)
            dedup_refs.append(ref)
    if len(dedup_refs) > _ALLOWED_REFS_CAP:
        dedup_refs = dedup_refs[:_ALLOWED_REFS_CAP]

    return ([{"section": "cycle_summary", "items": [payload]}], dedup_refs)


async def polish_run_journals(
    *,
    run_id: str,
    workspace_id: str = "ws_default",
    now: datetime | None = None,
) -> int:
    """Polish every non-empty journal entry of a run and persist the result.

    Behaviour:
      * No-op when `settings.brain_llm_polish_enabled` is false.
      * Skips rows with `is_empty=1` — there is nothing narrative-worthy.
      * Skips rows that already have `narrative_polished IS NOT NULL` (idempotent).
      * Skips rows with zero allowed_evidence_refs (grounding floor).
      * Failures swallowed and logged; deterministic body_json stays visible.

    Returns the count of rows updated.
    """
    # Lazy import to keep cycle.py importable without the LLM stack in tests
    # that monkey-patch the brain_llm subsystem.
    from core.api.config import settings as _settings
    from core.api.db import write_db
    from core.api.services.brain.llm.factory import (  # type: ignore
        BrainLLMConfigError,
        get_brain_llm_service,
    )
    from core.api.services.brain.llm.journal_polish import polish_journal_entry

    if not getattr(_settings, "brain_llm_polish_enabled", False):
        logger.info(
            "polish_run_journals skip reason=disabled run_id=%s", run_id
        )
        return 0

    try:
        service = get_brain_llm_service()
    except BrainLLMConfigError as exc:
        logger.warning(
            "polish_run_journals skip reason=misconfig run_id=%s detail=%s",
            run_id, exc,
        )
        return 0
    except Exception:  # noqa: BLE001 — never break the cycle
        logger.warning(
            "polish_run_journals skip reason=service_unavailable run_id=%s",
            run_id, exc_info=True,
        )
        return 0

    # Wave 3.1 polish UX fix (Emilio 2026-05-19): grounding_strict obbligava
    # il LLM a citare ≥1 evidence_ref dal whitelist e il check rifiutava
    # come `not_success` ~50% delle entries (~72% per scope=company aggregato).
    # Il body_json passato al LLM è già ground truth (eventi deterministici);
    # un secondo gate citation non previene hallucination reale ma blocca
    # narrative valide. Disabilitiamo by-default — `brain_llm_grounding_strict`
    # setting resta come escape hatch operatore via .env, ma default False.
    grounding_strict = bool(
        getattr(_settings, "brain_llm_grounding_strict", False)
    )
    now_iso = _utc_iso(now or datetime.now(timezone.utc))

    rows: list[tuple[str, str, str, str, str]] = []
    async with write_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT entry_id, scope_type, scope_key, body_json, cycle_key"
            " FROM brain_journal_entries"
            " WHERE run_id = ? AND workspace_id = ? AND is_empty = 0"
            "   AND narrative_polished IS NULL",
            (run_id, workspace_id),
        )
        async for row in cur:
            rows.append(
                (
                    row["entry_id"],
                    row["scope_type"],
                    row["scope_key"],
                    row["body_json"] or "{}",
                    row["cycle_key"],
                )
            )
        await cur.close()

    # Wave 3.1 silent-skip-v3 prevention (learning 46d8d1d4): emit INFO log at
    # start so journalctl always shows whether polish actually ran. Counts
    # below per skip reason close the diagnostic loop without re-deploying.
    logger.info(
        "polish_run_journals start run_id=%s rows=%d grounding_strict=%s",
        run_id, len(rows), grounding_strict,
    )

    updated = 0
    skipped_invalid_json = 0
    skipped_no_refs = 0
    skipped_polish_exception = 0
    skipped_polish_not_success = 0
    skipped_polish_empty = 0
    for entry_id, scope_type, scope_key, body_json, cycle_key in rows:
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError:
            skipped_invalid_json += 1
            continue
        # Wave 3.1 smart payload (Emilio 2026-05-19): query brain_digest_events
        # for this run + scope, apply reduction rules (decisions all / handoffs
        # cap 3 per project / commits aggregated / kg hotspots top 5 / others
        # aggregated). Output ~3KB JSON instead of the up-to-80KB body sources.
        async with write_db() as ev_db:
            events_for_polish = await _fetch_run_events(
                ev_db, run_id=run_id, scope_type=scope_type, scope_key=scope_key,
                cycle_key=cycle_key, workspace_id=workspace_id,
            )
        entries_payload, allowed_refs = _build_smart_polish_payload(
            events=events_for_polish, scope_type=scope_type, scope_key=scope_key
        )
        if not allowed_refs:
            skipped_no_refs += 1
            continue
        try:
            result = await polish_journal_entry(
                service=service,
                grounding_strict=grounding_strict,
                run_id=run_id,
                entry_id=entry_id,
                scope_type=scope_type,
                scope_key=scope_key,
                cycle_key=cycle_key,
                entries=entries_payload,
                allowed_evidence_refs=allowed_refs,
                deterministic_narrative=_polish_deterministic_narrative(body),
            )
        except Exception:  # noqa: BLE001 — never break the cycle
            logger.warning(
                "polish_run_journals_polish_failed entry_id=%s scope=%s/%s",
                entry_id,
                scope_type,
                scope_key,
                exc_info=True,
            )
            skipped_polish_exception += 1
            continue
        if not result.success:
            logger.info(
                "polish skip reason=not_success entry_id=%s scope=%s/%s "
                "purpose=%s fallback_reason=%s",
                entry_id, scope_type, scope_key,
                getattr(result, "purpose", "?"),
                getattr(result, "reason", "?"),
            )
            skipped_polish_not_success += 1
            continue
        polished_text = (result.polished or {}).get("narrative_polished") or None
        if not polished_text:
            skipped_polish_empty += 1
            continue
        async with write_db() as db:
            await db.execute(
                "UPDATE brain_journal_entries SET"
                "  narrative_polished = ?,"
                "  narrative_polished_at = ?,"
                "  narrative_polished_model = ?"
                " WHERE entry_id = ?",
                (polished_text, now_iso, result.model or "", entry_id),
            )
        updated += 1
    logger.info(
        "polish_run_journals done run_id=%s updated=%d "
        "skip{invalid_json=%d, no_refs=%d, polish_exception=%d, "
        "not_success=%d, empty=%d}",
        run_id, updated,
        skipped_invalid_json, skipped_no_refs,
        skipped_polish_exception, skipped_polish_not_success,
        skipped_polish_empty,
    )
    return updated


async def polish_pending_journals(
    *,
    workspace_id: str = "ws_default",
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backfill polish for historic non-empty entries with `narrative_polished IS NULL`.

    Unlike `polish_run_journals` (which filters by `run_id` and runs inline
    after `publish_run_journals`), this helper scans ALL entries across all
    runs that still need polish. Built to recover the cohort that landed in
    DB before the X-Sync header fix (silent-skip v3).

    Cap `limit` per call so callers can paginate from cron / curl without
    holding the writer lock for minutes. Returns a structured envelope:
    `{updated, skip_*, remaining}` so the operator knows when to stop calling.
    """
    from core.api.config import settings as _settings
    from core.api.db import write_db
    from core.api.services.brain.llm.factory import (  # type: ignore
        BrainLLMConfigError,
        get_brain_llm_service,
    )
    from core.api.services.brain.llm.journal_polish import polish_journal_entry

    if not getattr(_settings, "brain_llm_polish_enabled", False):
        logger.info("polish_pending_journals skip reason=disabled")
        return {"updated": 0, "remaining": 0, "skipped_reason": "disabled"}

    try:
        service = get_brain_llm_service()
    except BrainLLMConfigError as exc:
        logger.warning("polish_pending_journals skip reason=misconfig detail=%s", exc)
        return {"updated": 0, "remaining": 0, "skipped_reason": f"misconfig:{exc}"}
    except Exception:  # noqa: BLE001 — never break the cycle
        logger.warning("polish_pending_journals skip reason=service_unavailable", exc_info=True)
        return {"updated": 0, "remaining": 0, "skipped_reason": "service_unavailable"}

    # Wave 3.1 polish UX fix (Emilio 2026-05-19): grounding_strict default
    # disabilitato — vedi nota in `polish_run_journals` sopra.
    grounding_strict = bool(getattr(_settings, "brain_llm_grounding_strict", False))
    now_iso = _utc_iso(now or datetime.now(timezone.utc))

    # Fetch a bounded slice of pending entries — newest first so the operator
    # sees recent cycles polished before deep history.
    rows: list[tuple[str, str, str, str, str, str]] = []
    async with write_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT entry_id, run_id, scope_type, scope_key, body_json, cycle_key"
            " FROM brain_journal_entries"
            " WHERE workspace_id = ? AND is_empty = 0"
            "   AND narrative_polished IS NULL"
            " ORDER BY cycle_key DESC, scope_type ASC, scope_key ASC"
            " LIMIT ?",
            (workspace_id, int(limit)),
        )
        async for row in cur:
            rows.append(
                (
                    row["entry_id"],
                    row["run_id"],
                    row["scope_type"],
                    row["scope_key"],
                    row["body_json"] or "{}",
                    row["cycle_key"],
                )
            )
        await cur.close()
        remaining_cur = await db.execute(
            "SELECT count(*) FROM brain_journal_entries"
            " WHERE workspace_id = ? AND is_empty = 0"
            "   AND narrative_polished IS NULL",
            (workspace_id,),
        )
        remaining_row = await remaining_cur.fetchone()
        await remaining_cur.close()
    total_pending = int(remaining_row[0] if remaining_row else 0)

    logger.info(
        "polish_pending_journals start picked=%d remaining=%d limit=%d grounding_strict=%s",
        len(rows), total_pending, limit, grounding_strict,
    )

    updated = 0
    skipped_invalid_json = 0
    skipped_no_refs = 0
    skipped_polish_exception = 0
    skipped_polish_not_success = 0
    skipped_polish_empty = 0
    for entry_id, run_id, scope_type, scope_key, body_json, cycle_key in rows:
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError:
            skipped_invalid_json += 1
            continue
        # Wave 3.1 smart payload (Emilio 2026-05-19): see polish_run_journals.
        async with write_db() as ev_db:
            events_for_polish = await _fetch_run_events(
                ev_db, run_id=run_id, scope_type=scope_type, scope_key=scope_key,
                cycle_key=cycle_key, workspace_id=workspace_id,
            )
        entries_payload, allowed_refs = _build_smart_polish_payload(
            events=events_for_polish, scope_type=scope_type, scope_key=scope_key
        )
        if not allowed_refs:
            skipped_no_refs += 1
            continue
        try:
            result = await polish_journal_entry(
                service=service,
                grounding_strict=grounding_strict,
                run_id=run_id,
                entry_id=entry_id,
                scope_type=scope_type,
                scope_key=scope_key,
                cycle_key=cycle_key,
                entries=entries_payload,
                allowed_evidence_refs=allowed_refs,
                deterministic_narrative=_polish_deterministic_narrative(body),
            )
        except Exception:  # noqa: BLE001 — never break the cycle
            logger.warning(
                "polish_pending_journals polish_failed entry_id=%s scope=%s/%s",
                entry_id, scope_type, scope_key,
                exc_info=True,
            )
            skipped_polish_exception += 1
            continue
        if not result.success:
            logger.info(
                "polish_pending skip reason=not_success entry_id=%s scope=%s/%s "
                "purpose=%s fallback_reason=%s",
                entry_id, scope_type, scope_key,
                getattr(result, "purpose", "?"),
                getattr(result, "reason", "?"),
            )
            skipped_polish_not_success += 1
            continue
        polished_text = (result.polished or {}).get("narrative_polished") or None
        if not polished_text:
            skipped_polish_empty += 1
            continue
        async with write_db() as db:
            await db.execute(
                "UPDATE brain_journal_entries SET"
                "  narrative_polished = ?,"
                "  narrative_polished_at = ?,"
                "  narrative_polished_model = ?"
                " WHERE entry_id = ?",
                (polished_text, now_iso, result.model or "", entry_id),
            )
        updated += 1
    remaining_after = max(0, total_pending - updated)
    logger.info(
        "polish_pending_journals done updated=%d remaining=%d "
        "skip{invalid_json=%d, no_refs=%d, polish_exception=%d, "
        "not_success=%d, empty=%d}",
        updated, remaining_after,
        skipped_invalid_json, skipped_no_refs,
        skipped_polish_exception, skipped_polish_not_success,
        skipped_polish_empty,
    )
    return {
        "updated": updated,
        "remaining": remaining_after,
        "skipped": {
            "invalid_json": skipped_invalid_json,
            "no_refs": skipped_no_refs,
            "polish_exception": skipped_polish_exception,
            "not_success": skipped_polish_not_success,
            "empty": skipped_polish_empty,
        },
    }
