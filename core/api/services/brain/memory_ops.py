# Brain v1 — Memory Operations service (sub-03 L4 — §4/§5/§10).
#
# Memory-Ops runs as PHASE 4 of the same brain_runs envelope (after Drift,
# before Findings). The orchestrator:
#   1. Builds an OpSnapshot (read-only L2/L3 projection — digest events +
#      journal entries + drift signals for the current run_id).
#   2. Invokes each M-rule (M1-M7) with a 15s budget; per-rule failures
#      isolated and reported via partial_failures_json.
#   3. Persists operations via INSERT OR IGNORE (BLAKE2b stable id).
#   4. Updates supersede chain across prior pending operations sharing
#      the same recurrence_key.
#
# Layering invariants (parent §9, sub-03 §7):
#   * NO LLM imports (parent §9.3). AST-grep test enforces.
#   * NO raw SQL on substrate (tasks/PR/handoffs/learnings/kg_edges).
#   * NO mutation of substrate from this module.
#   * Memory-Ops NEVER re-reads substrate — only L2 events + L3 signals
#     scoped to the current run_id (`cycle_snapshot` pattern).
#   * Stable BLAKE2b 16-byte operation_id. EXCLUDES from hash: score,
#     summary, approval_state, proposed_write payload, classifier outputs.
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import aiosqlite

from core.api.db import acquire_db, write_db
from core.api.models import UserInfo
from core.api.models.brain import (
    ApplyNextAction,
    ApplyResponse,
    ApprovalState,
    MemoryOperation,
    MemoryOperationRedacted,
    MemoryOperationsListResponse,
    MyelinDirection,
    MyelinEffect,
    OperationType,
    ProposedWrite,
    ProposedWriteTarget,
    ScopeTypeL4,
)
from core.api.services.brain.compound_bridge import (
    DEFAULT_TARGET_FOR_OP,
    build_proposed_write_doc_patch,
    build_proposed_write_kg_edge_metric,
    build_proposed_write_task,
    proposed_write_none,
)
from core.api.services.brain.edge_metrics import compute_reinforce_score
from core.api.visibility import get_visible_projects

logger = logging.getLogger(__name__)

DEFAULT_RULE_TIMEOUT_S = 15
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
BULK_PATCH_MAX = 25
EXPIRY_DAYS_DEFAULT = 30

# Direction matrix — derived from operation_type (§4.X). NOT a persisted column.
OPERATION_DIRECTION: dict[OperationType, MyelinDirection] = {
    "reinforce": "strengthen",
    "consolidate": "connect",
    "supersede_candidate": "split",
    "provenance_hardening": "strengthen",
    "orphan_detected": "quarantine_candidate",
    "contradiction_detected": "split",
    "cascade_rollup": "canonicalize",
    "compression_candidate": "connect",
    "deduplicate": "connect",
    "promotion_candidate": "promote",
}


# ---------------------------------------------------------------------------
# Utilities (deterministic)
# ---------------------------------------------------------------------------


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def canonical_evidence(evidence: Iterable[str | dict]) -> str:
    """Stable JSON for hash. Mirror sub-02 helper (intentionally duplicated to
    avoid importing a private helper across layers)."""
    norm: list[str] = []
    for item in evidence:
        if isinstance(item, str):
            norm.append(item)
        else:
            norm.append(
                json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            )
    norm.sort()
    return json.dumps(norm, sort_keys=False, ensure_ascii=False, separators=(",", ":"))


def evidence_hash(evidence: Iterable[str | dict]) -> str:
    """sha256 64-char hex per sub-03 §4.1 contract."""
    return hashlib.sha256(canonical_evidence(evidence).encode("utf-8")).hexdigest()


def make_operation_id(
    *,
    cycle_key: str,
    operation_type: OperationType,
    scope_type: ScopeTypeL4,
    scope_key: str,
    source_ref: str,
    target_ref: str,
    evidence_hash_hex: str,
) -> str:
    """Stable BLAKE2b-16 hex (sub-03 §4.2).

    EXCLUDES: score, approval_state, summary text, proposed_write payload,
    classifier_version, severity, confidence. Recompute idempotent across
    interpreter runs given identical natural-key inputs.
    """
    payload = (
        f"{cycle_key}|{operation_type}|{scope_type}|{scope_key}|"
        f"{source_ref}|{target_ref}|{evidence_hash_hex}"
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def make_recurrence_key(
    *,
    operation_type: OperationType,
    scope_type: ScopeTypeL4,
    scope_key: str,
    source_ref: str,
    target_ref: str,
) -> str:
    """BLAKE2b-8 hex without cycle_key — groups same op across cycles."""
    payload = (
        f"{operation_type}|{scope_type}|{scope_key}|{source_ref}|{target_ref}"
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DigestEventRow:
    event_id: str
    cycle_key: str
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
    entry_id: str
    cycle_key: str
    scope_type: str
    scope_key: str
    program_key: str | None
    body: dict[str, Any]
    is_empty: bool
    published_at: datetime


@dataclass(slots=True, frozen=True)
class DriftSignalRow:
    signal_id: str
    cycle_key: str
    rule_id: str
    signal_type: str
    knowledge_form: str
    scope_type: str
    scope_key: str
    program_key: str | None
    observed_direction_ref: str
    severity: str
    confidence: float
    recurrence_key: str
    involved_projects: list[str]


@dataclass(slots=True, frozen=True)
class OpSnapshot:
    """Read-only L2+L3 projection consumed by all M-rules."""

    cycle_key: str
    run_id: str
    workspace_id: str
    as_of: datetime
    events: tuple[DigestEventRow, ...]
    journal_entries: tuple[JournalEntryRow, ...]
    drift_signals: tuple[DriftSignalRow, ...]

    events_by_source_ref: dict[str, list[DigestEventRow]] = field(default_factory=dict)
    drift_by_observed_ref: dict[str, list[DriftSignalRow]] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryOpsReport:
    """Return envelope for memory_ops.run_phase()."""

    run_id: str
    cycle_key: str
    operation_count: int = 0
    partial_failures: list[dict[str, str]] = field(default_factory=list)


def _parse_evidence(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _parse_body(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _parse_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


async def build_snapshot(
    *,
    run_id: str,
    cycle_key: str,
    workspace_id: str = "ws_default",
    as_of: datetime | None = None,
) -> OpSnapshot:
    """Read-only projection: digest events + journal entries + drift signals
    for the current run_id only. Memory-Ops NEVER re-reads substrate.
    """
    now = as_of or datetime.now(timezone.utc)
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        ev_rows = await (
            await db.execute(
                "SELECT event_id, cycle_key, event_type, source_system, source_project,"
                " target_project, program_key, source_ref, title, summary, observed_at,"
                " evidence_json FROM brain_digest_events WHERE run_id = ?",
                (run_id,),
            )
        ).fetchall()
        jn_rows = await (
            await db.execute(
                "SELECT entry_id, cycle_key, scope_type, scope_key, program_key,"
                " body_json, is_empty, published_at"
                " FROM brain_journal_entries WHERE run_id = ?",
                (run_id,),
            )
        ).fetchall()
        dr_rows = await (
            await db.execute(
                "SELECT signal_id, cycle_key, rule_id, signal_type, knowledge_form,"
                " scope_type, scope_key, program_key, observed_direction_ref,"
                " severity, confidence, recurrence_key, involved_projects_json"
                " FROM brain_drift_signals WHERE run_id = ?",
                (run_id,),
            )
        ).fetchall()

    events: list[DigestEventRow] = []
    by_source_ref: dict[str, list[DigestEventRow]] = defaultdict(list)
    for r in ev_rows:
        obs_at = _parse_iso(r["observed_at"]) or now
        row = DigestEventRow(
            event_id=r["event_id"],
            cycle_key=r["cycle_key"],
            event_type=r["event_type"],
            source_system=r["source_system"],
            source_project=r["source_project"],
            target_project=r["target_project"],
            program_key=r["program_key"],
            source_ref=r["source_ref"],
            title=r["title"] or "",
            summary=r["summary"] or "",
            observed_at=obs_at,
            evidence=_parse_evidence(r["evidence_json"]),
        )
        events.append(row)
        by_source_ref[row.source_ref].append(row)

    journal: list[JournalEntryRow] = []
    for r in jn_rows:
        published_at = _parse_iso(r["published_at"]) or now
        journal.append(
            JournalEntryRow(
                entry_id=r["entry_id"],
                cycle_key=r["cycle_key"],
                scope_type=r["scope_type"],
                scope_key=r["scope_key"],
                program_key=r["program_key"],
                body=_parse_body(r["body_json"]),
                is_empty=bool(r["is_empty"]),
                published_at=published_at,
            )
        )

    signals: list[DriftSignalRow] = []
    by_observed_ref: dict[str, list[DriftSignalRow]] = defaultdict(list)
    for r in dr_rows:
        involved = _parse_list(r["involved_projects_json"])
        ds = DriftSignalRow(
            signal_id=r["signal_id"],
            cycle_key=r["cycle_key"],
            rule_id=r["rule_id"],
            signal_type=r["signal_type"],
            knowledge_form=r["knowledge_form"],
            scope_type=r["scope_type"],
            scope_key=r["scope_key"],
            program_key=r["program_key"],
            observed_direction_ref=r["observed_direction_ref"],
            severity=r["severity"],
            confidence=float(r["confidence"]),
            recurrence_key=r["recurrence_key"],
            involved_projects=[str(x) for x in involved],
        )
        signals.append(ds)
        by_observed_ref[ds.observed_direction_ref].append(ds)

    return OpSnapshot(
        cycle_key=cycle_key,
        run_id=run_id,
        workspace_id=workspace_id,
        as_of=now,
        events=tuple(events),
        journal_entries=tuple(journal),
        drift_signals=tuple(signals),
        events_by_source_ref=dict(by_source_ref),
        drift_by_observed_ref=dict(by_observed_ref),
    )


# ---------------------------------------------------------------------------
# Operation builder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OperationDraft:
    """Raw rule output before stable-id derivation + persistence."""

    operation_type: OperationType
    scope_type: ScopeTypeL4
    scope_key: str
    program_key: str | None
    source_ref: str
    target_ref: str
    evidence: list[str]
    summary: str
    proposed_write: ProposedWrite
    involved_projects: list[str] = field(default_factory=list)
    score: float | None = None


def _score_for(
    *,
    evidence: list[str],
    drift_refs_present: bool,
    recurrence_count: int,
    pins_hit: bool = False,
    multi_cycle: bool = False,
) -> float:
    return compute_reinforce_score(
        evidence=evidence,
        drift_refs_present=drift_refs_present,
        recurrence_count=recurrence_count,
        pins_hit=pins_hit,
        multi_cycle=multi_cycle,
    )


def finalize_operation(
    *,
    draft: OperationDraft,
    run_id: str,
    cycle_key: str,
    now: datetime,
    recurrence_count: int = 1,
    first_seen_cycle_key: str | None = None,
    expiry_days: int = EXPIRY_DAYS_DEFAULT,
) -> MemoryOperation:
    """Derive id/hash/score and construct a MemoryOperation Pydantic instance."""
    evidence_sorted = sorted(set(draft.evidence))
    ev_hash = evidence_hash(evidence_sorted)
    drift_present = any(ev.startswith("drift:") for ev in evidence_sorted)
    score = draft.score if draft.score is not None else _score_for(
        evidence=evidence_sorted,
        drift_refs_present=drift_present,
        recurrence_count=recurrence_count,
    )
    operation_id = make_operation_id(
        cycle_key=cycle_key,
        operation_type=draft.operation_type,
        scope_type=draft.scope_type,
        scope_key=draft.scope_key,
        source_ref=draft.source_ref,
        target_ref=draft.target_ref,
        evidence_hash_hex=ev_hash,
    )
    recurrence_key = make_recurrence_key(
        operation_type=draft.operation_type,
        scope_type=draft.scope_type,
        scope_key=draft.scope_key,
        source_ref=draft.source_ref,
        target_ref=draft.target_ref,
    )
    direction = OPERATION_DIRECTION.get(draft.operation_type, "connect")
    return MemoryOperation(
        operation_id=operation_id,
        run_id=run_id,
        cycle_key=cycle_key,
        detected_at=now.astimezone(timezone.utc),
        operation_type=draft.operation_type,
        schema_version=1,
        scope_type=draft.scope_type,
        scope_key=draft.scope_key,
        program_key=draft.program_key,
        source_ref=draft.source_ref,
        target_ref=draft.target_ref,
        score=score,
        recurrence_key=recurrence_key,
        recurrence_count=recurrence_count,
        first_seen_cycle_key=first_seen_cycle_key or cycle_key,
        last_seen_cycle_key=cycle_key,
        involved_projects=sorted(set(draft.involved_projects)),
        evidence=evidence_sorted,
        evidence_hash=ev_hash,
        summary=draft.summary[:2000],
        proposed_write=draft.proposed_write,
        myelin_effect=MyelinEffect(direction=direction, score=score),
        approval_state="pending",
        expires_at=(now + timedelta(days=expiry_days)).astimezone(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Rule builders (M1-M7) — deterministic, no LLM.
# ---------------------------------------------------------------------------


def _project_scope(slug: str | None) -> tuple[ScopeTypeL4, str]:
    if slug:
        return ("project", slug)
    return ("company", "__company__")


async def _m1_reinforce(snapshot: OpSnapshot, *, run_id: str, now: datetime, min_count: int = 5) -> list[OperationDraft]:
    """M1 reinforce: same source_ref cited by >=min_count events or drift."""
    drafts: list[OperationDraft] = []
    counts: dict[str, list[str]] = defaultdict(list)
    project_for_ref: dict[str, str | None] = {}
    program_for_ref: dict[str, str | None] = {}
    for ev in snapshot.events:
        counts[ev.source_ref].append(f"event:{ev.event_id}")
        project_for_ref.setdefault(ev.source_ref, ev.source_project)
        program_for_ref.setdefault(ev.source_ref, ev.program_key)
    for sig in snapshot.drift_signals:
        counts[sig.observed_direction_ref].append(f"drift:{sig.signal_id}")
    for ref, evs in counts.items():
        if len(evs) < min_count:
            continue
        scope_type, scope_key = _project_scope(project_for_ref.get(ref))
        involved = [project_for_ref[ref]] if project_for_ref.get(ref) else []
        drift_present = any(e.startswith("drift:") for e in evs)
        score = compute_reinforce_score(
            evidence=evs,
            drift_refs_present=drift_present,
            recurrence_count=1,
        )
        payload = build_proposed_write_kg_edge_metric(
            edge_id=ref,
            score=score,
        )
        drafts.append(
            OperationDraft(
                operation_type="reinforce",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=program_for_ref.get(ref),
                source_ref=ref,
                target_ref="",
                evidence=evs,
                summary=f"Reinforce {ref}: {len(evs)} citations this cycle.",
                proposed_write=payload,
                involved_projects=[p for p in involved if p],
                score=score,
            )
        )
    return drafts


async def _m2_consolidate(snapshot: OpSnapshot, *, run_id: str, now: datetime) -> list[OperationDraft]:
    """M2 consolidate: multiple events with the same evidence hash. v1
    heuristic — duplicate title within scope on the same cycle."""
    drafts: list[OperationDraft] = []
    title_groups: dict[tuple[str, str | None], list[DigestEventRow]] = defaultdict(list)
    for ev in snapshot.events:
        if ev.title:
            title_groups[(ev.title.strip().lower(), ev.source_project)].append(ev)
    for (_, project), members in title_groups.items():
        if len(members) < 2:
            continue
        sorted_members = sorted(members, key=lambda m: m.observed_at)
        canonical = sorted_members[0]
        # Self-loop guard: skip groups where canonical and first duplicate
        # share the same source_ref (same artifact transitioning, not real
        # duplication). CHECK constraint enforces, but emitting and catching
        # would degrade run status.
        distinct = [m for m in sorted_members[1:] if m.source_ref != canonical.source_ref]
        if not distinct:
            continue
        duplicates = distinct
        scope_type, scope_key = _project_scope(project)
        evidence_refs = [f"event:{m.event_id}" for m in sorted_members]
        payload = build_proposed_write_doc_patch(
            path=canonical.source_ref,
            unified_diff="--- a/duplicate\n+++ b/canonical\n@@\n# Consolidate evidence\n",
            base_sha="",
            rationale=(
                f"Multiple events share title '{canonical.title[:80]}'. "
                f"Canonical: {canonical.source_ref}; duplicates: "
                + ", ".join(d.source_ref for d in duplicates)
            ),
        )
        drafts.append(
            OperationDraft(
                operation_type="consolidate",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=canonical.program_key,
                source_ref=canonical.source_ref,
                target_ref=duplicates[0].source_ref,
                evidence=evidence_refs,
                summary=(
                    f"Consolidate {len(duplicates)} duplicates of '{canonical.title[:80]}' "
                    f"into {canonical.source_ref}."
                ),
                proposed_write=payload,
                involved_projects=[project] if project else [],
            )
        )
    return drafts


async def _m3_consolidate_edges(snapshot: OpSnapshot, *, run_id: str, now: datetime) -> list[OperationDraft]:
    """M3 edge metrics compatibility — runs alongside M1 as the future-edge
    shim. v1: no-op; the reinforce score becomes ProposedWriteKGEdgeMetric.
    Kept as separate rule for AST-grep + future expansion (decay/touch_count).
    """
    return []


async def _m4_supersede(snapshot: OpSnapshot, *, run_id: str, now: datetime) -> list[OperationDraft]:
    """M4 supersede: drift signals with rule_id=DR2 (decision_without_adr)
    flagging a doc that contradicts a newer one — propose supersession.
    Heuristic v1: pair DR4 (docs_governance_drift) signals where two refs
    in evidence both look like docs and one is newer.
    """
    drafts: list[OperationDraft] = []
    for sig in snapshot.drift_signals:
        if sig.signal_type != "docs_governance_drift":
            continue
        observed = sig.observed_direction_ref
        evidence_refs = [f"drift:{sig.signal_id}"]
        scope_type, scope_key = sig.scope_type, sig.scope_key  # type: ignore[assignment]
        if scope_type not in ("company", "program", "project", "artifact"):
            scope_type = "company"
            scope_key = "__company__"
        drafts.append(
            OperationDraft(
                operation_type="supersede_candidate",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=sig.program_key,
                source_ref=observed,
                target_ref="",
                evidence=evidence_refs,
                summary=f"Supersede candidate: docs governance drift on {observed}.",
                proposed_write=build_proposed_write_doc_patch(
                    path=observed,
                    unified_diff="--- a/old\n+++ b/new\n@@\n# Flag for supersede review\n",
                    base_sha="",
                    rationale=f"Drift signal {sig.signal_id} flags {observed} as governance-drift.",
                ),
                involved_projects=list(sig.involved_projects),
            )
        )
    return drafts


async def _m5_provenance(snapshot: OpSnapshot, *, run_id: str, now: datetime) -> list[OperationDraft]:
    """M5 provenance hardening: drift signals on knowledge_form='tribal_memory'
    or 'unknown' propose a task to attach proper provenance."""
    drafts: list[OperationDraft] = []
    for sig in snapshot.drift_signals:
        if sig.knowledge_form not in ("tribal_memory", "unknown"):
            continue
        scope_type, scope_key = sig.scope_type, sig.scope_key  # type: ignore[assignment]
        if scope_type not in ("company", "program", "project", "artifact"):
            scope_type = "company"
            scope_key = "__company__"
        project_for_task = sig.scope_key if scope_type == "project" else "marvisx"
        op_id_seed = make_operation_id(
            cycle_key=snapshot.cycle_key,
            operation_type="provenance_hardening",
            scope_type=scope_type,  # type: ignore[arg-type]
            scope_key=scope_key,
            source_ref=sig.observed_direction_ref,
            target_ref="",
            evidence_hash_hex=evidence_hash([f"drift:{sig.signal_id}"]),
        )
        payload = build_proposed_write_task(
            operation_id=op_id_seed,
            title=f"Harden provenance for {sig.observed_direction_ref}",
            description=(
                f"Drift signal {sig.signal_id} (form={sig.knowledge_form}) flags "
                f"{sig.observed_direction_ref}. Attach task/PR/source chain."
            ),
            project=project_for_task,
            impact=5,
            confidence=5,
            ease=6,
        )
        drafts.append(
            OperationDraft(
                operation_type="provenance_hardening",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=sig.program_key,
                source_ref=sig.observed_direction_ref,
                target_ref="",
                evidence=[f"drift:{sig.signal_id}"],
                summary=(
                    f"Provenance hardening for {sig.observed_direction_ref}: "
                    f"knowledge_form={sig.knowledge_form}."
                ),
                proposed_write=payload,
                involved_projects=list(sig.involved_projects),
            )
        )
    return drafts


async def _m6_orphan(snapshot: OpSnapshot, *, run_id: str, now: datetime) -> list[OperationDraft]:
    """M6 orphan detected: digest events without source_project or program_key."""
    drafts: list[OperationDraft] = []
    seen: set[str] = set()
    for ev in snapshot.events:
        if ev.source_project or ev.program_key:
            continue
        if ev.source_ref in seen:
            continue
        seen.add(ev.source_ref)
        op_id_seed = make_operation_id(
            cycle_key=snapshot.cycle_key,
            operation_type="orphan_detected",
            scope_type="company",
            scope_key="__company__",
            source_ref=ev.source_ref,
            target_ref="",
            evidence_hash_hex=evidence_hash([f"event:{ev.event_id}"]),
        )
        payload = build_proposed_write_task(
            operation_id=op_id_seed,
            title=f"Triage orphan {ev.source_ref}",
            description=(
                f"Event {ev.event_id} ({ev.event_type}) has no project/program. "
                "Assign or archive."
            ),
            project="marvisx",
            impact=3,
            confidence=6,
            ease=7,
        )
        drafts.append(
            OperationDraft(
                operation_type="orphan_detected",
                scope_type="company",
                scope_key="__company__",
                program_key=None,
                source_ref=ev.source_ref,
                target_ref="",
                evidence=[f"event:{ev.event_id}"],
                summary=f"Orphan: {ev.source_ref} ({ev.event_type}).",
                proposed_write=payload,
                involved_projects=[],
            )
        )
    return drafts


async def _m7_contradiction(snapshot: OpSnapshot, *, run_id: str, now: datetime) -> list[OperationDraft]:
    """M7 contradiction: same source_ref with mixed decision_marker/observed_delta
    polarity. v1 heuristic — DR2 (decision_without_adr) signals on same scope."""
    drafts: list[OperationDraft] = []
    by_scope: dict[tuple[str, str], list[DriftSignalRow]] = defaultdict(list)
    for sig in snapshot.drift_signals:
        if sig.signal_type != "decision_without_adr":
            continue
        by_scope[(sig.scope_type, sig.scope_key)].append(sig)
    for (scope_type, scope_key), members in by_scope.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda s: s.signal_id)
        first, second = ordered[0], ordered[1]
        if first.observed_direction_ref == second.observed_direction_ref:
            continue
        if scope_type not in ("company", "program", "project", "artifact"):
            scope_type_lit: ScopeTypeL4 = "company"
            scope_key = "__company__"
        else:
            scope_type_lit = scope_type  # type: ignore[assignment]
        op_id_seed = make_operation_id(
            cycle_key=snapshot.cycle_key,
            operation_type="contradiction_detected",
            scope_type=scope_type_lit,
            scope_key=scope_key,
            source_ref=first.observed_direction_ref,
            target_ref=second.observed_direction_ref,
            evidence_hash_hex=evidence_hash(
                [f"drift:{first.signal_id}", f"drift:{second.signal_id}"]
            ),
        )
        proj_for_task = scope_key if scope_type_lit == "project" else "marvisx"
        payload = build_proposed_write_task(
            operation_id=op_id_seed,
            title="Resolve contradiction between decisions",
            description=(
                f"Decisions {first.observed_direction_ref} and "
                f"{second.observed_direction_ref} appear in the same scope "
                "without an ADR reconciling them."
            ),
            project=proj_for_task,
            impact=6,
            confidence=5,
            ease=4,
        )
        drafts.append(
            OperationDraft(
                operation_type="contradiction_detected",
                scope_type=scope_type_lit,
                scope_key=scope_key,
                program_key=first.program_key,
                source_ref=first.observed_direction_ref,
                target_ref=second.observed_direction_ref,
                evidence=[f"drift:{first.signal_id}", f"drift:{second.signal_id}"],
                summary=(
                    f"Contradiction in {scope_type_lit}/{scope_key}: "
                    f"{first.observed_direction_ref} vs {second.observed_direction_ref}."
                ),
                proposed_write=payload,
                involved_projects=sorted(
                    set(first.involved_projects) | set(second.involved_projects)
                ),
            )
        )
    return drafts


REGISTERED_RULES: tuple[tuple[str, Any], ...] = (
    ("M1", _m1_reinforce),
    ("M2", _m2_consolidate),
    ("M3", _m3_consolidate_edges),
    ("M4", _m4_supersede),
    ("M5", _m5_provenance),
    ("M6", _m6_orphan),
    ("M7", _m7_contradiction),
)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _serialize_proposed_write(pw: ProposedWrite) -> tuple[str, str]:
    """Return (target_type, json_str)."""
    if hasattr(pw, "model_dump_json"):
        data = pw.model_dump_json()
        target = pw.target_type  # type: ignore[union-attr]
        return (target, data)
    raise TypeError(f"proposed_write must be a Pydantic model, got {type(pw)!r}")


async def _fetch_existing_operation_id(
    db: aiosqlite.Connection, operation_id: str
) -> aiosqlite.Row | None:
    return await (
        await db.execute(
            "SELECT operation_id, recurrence_count, first_seen_cycle_key,"
            " approval_state FROM brain_memory_operations WHERE operation_id = ?",
            (operation_id,),
        )
    ).fetchone()


async def _persist_operations(
    *, run_id: str, operations: list[MemoryOperation]
) -> tuple[int, list[str]]:
    if not operations:
        return (0, [])
    persisted = 0
    recurrence_keys: list[str] = []
    async with write_db() as db:
        for op in operations:
            existing = await _fetch_existing_operation_id(db, op.operation_id)
            target_type, pw_json = _serialize_proposed_write(op.proposed_write)
            if existing is not None:
                # Stable id collision → recompute. Bump last_seen_cycle_key
                # and recurrence_count if still pending; preserve approval.
                old_state = existing["approval_state"] if hasattr(existing, "keys") else existing[3]
                if old_state == "pending":
                    await db.execute(
                        "UPDATE brain_memory_operations SET"
                        "  last_seen_cycle_key = ?,"
                        "  recurrence_count = recurrence_count + 1"
                        " WHERE operation_id = ?",
                        (op.cycle_key, op.operation_id),
                    )
                continue
            await db.execute(
                "INSERT INTO brain_memory_operations ("
                " operation_id, run_id, cycle_key, detected_at, operation_type,"
                " schema_version, scope_type, scope_key, program_key, source_ref,"
                " target_ref, score, recurrence_key, recurrence_count,"
                " first_seen_cycle_key, last_seen_cycle_key, involved_projects_json,"
                " evidence_hash, summary, proposed_write_target_type,"
                " proposed_write_json, requires_approval, approval_state, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, 1, 'pending', ?)",
                (
                    op.operation_id,
                    op.run_id,
                    op.cycle_key,
                    _utc_iso(op.detected_at),
                    op.operation_type,
                    op.schema_version,
                    op.scope_type,
                    op.scope_key,
                    op.program_key,
                    op.source_ref,
                    op.target_ref,
                    op.score,
                    op.recurrence_key,
                    op.recurrence_count,
                    op.first_seen_cycle_key,
                    op.last_seen_cycle_key,
                    json.dumps(op.involved_projects, sort_keys=True, ensure_ascii=False),
                    op.evidence_hash,
                    op.summary,
                    target_type,
                    pw_json,
                    _utc_iso(op.expires_at),
                ),
            )
            # Audit row: initial pending state.
            await db.execute(
                "INSERT INTO brain_memory_operation_states ("
                " state_id, operation_id, from_state, to_state, actor_user_id, reason"
                ") VALUES (?, ?, NULL, 'pending', NULL, NULL)",
                (uuid.uuid4().hex, op.operation_id),
            )
            # Evidence join rows.
            for pos, ev in enumerate(op.evidence):
                kind = "digest_event"
                ref = ev
                if ev.startswith("event:"):
                    kind, ref = "digest_event", ev[len("event:") :]
                elif ev.startswith("drift:"):
                    kind, ref = "drift_signal", ev[len("drift:") :]
                elif ev.startswith("journal:"):
                    kind, ref = "journal_entry", ev[len("journal:") :]
                elif ev.startswith("handoff:"):
                    kind, ref = "handoff", ev[len("handoff:") :]
                elif ev.startswith("learning:"):
                    kind, ref = "learning", ev[len("learning:") :]
                elif ev.startswith("kg:"):
                    kind, ref = "kg_node", ev[len("kg:") :]
                elif ev.startswith("task:"):
                    kind, ref = "task", ev[len("task:") :]
                elif ev.startswith("pr:"):
                    kind, ref = "pr", ev[len("pr:") :]
                elif ev.startswith("commit:"):
                    kind, ref = "commit", ev[len("commit:") :]
                elif ev.startswith("audit:"):
                    kind, ref = "audit_log", ev[len("audit:") :]
                await db.execute(
                    "INSERT OR IGNORE INTO brain_memory_operation_evidence ("
                    " operation_id, position, evidence_kind, evidence_ref,"
                    " weight, cycle_key"
                    ") VALUES (?, ?, ?, ?, 1.0, ?)",
                    (op.operation_id, pos, kind, ref, op.cycle_key),
                )
            persisted += 1
            recurrence_keys.append(op.recurrence_key)
    return (persisted, recurrence_keys)


async def _supersede_prior(*, run_id: str, recurrence_keys: list[str]) -> int:
    if not recurrence_keys:
        return 0
    superseded = 0
    async with write_db() as db:
        for rkey in set(recurrence_keys):
            new_row = await (
                await db.execute(
                    "SELECT operation_id FROM brain_memory_operations"
                    " WHERE run_id = ? AND recurrence_key = ?",
                    (run_id, rkey),
                )
            ).fetchone()
            if new_row is None:
                continue
            new_op_id = new_row[0] if not hasattr(new_row, "keys") else new_row["operation_id"]
            await db.execute(
                "UPDATE brain_memory_operations SET"
                "  approval_state = 'superseded',"
                "  superseded_by_operation_id = ?"
                " WHERE recurrence_key = ?"
                "  AND approval_state = 'pending'"
                "  AND operation_id <> ?",
                (new_op_id, rkey, new_op_id),
            )
            superseded += 1
    return superseded


async def _run_one_rule(
    rule_id: str,
    builder,
    *,
    snapshot: OpSnapshot,
    run_id: str,
    now: datetime,
    timeout_s: int,
) -> list[OperationDraft]:
    async with asyncio.timeout(timeout_s):
        result = await builder(snapshot, run_id=run_id, now=now)
        return list(result)


async def run_phase(
    *,
    run_id: str,
    cycle_key: str,
    workspace_id: str = "ws_default",
    now: datetime | None = None,
    rule_timeout_s: int = DEFAULT_RULE_TIMEOUT_S,
    include_cascade: bool = False,
) -> MemoryOpsReport:
    """Memory-Ops phase entry. Caller (jobs._execute_cycle) invokes AFTER
    the Drift phase has produced signals for this run_id.
    """
    started = datetime.now(timezone.utc)
    now = (now or started).astimezone(timezone.utc)
    snapshot = await build_snapshot(
        run_id=run_id, cycle_key=cycle_key, workspace_id=workspace_id, as_of=now,
    )

    all_operations: list[MemoryOperation] = []
    partial_failures: list[dict[str, str]] = []

    for rule_id, builder in REGISTERED_RULES:
        try:
            drafts = await _run_one_rule(
                rule_id,
                builder,
                snapshot=snapshot,
                run_id=run_id,
                now=now,
                timeout_s=rule_timeout_s,
            )
        except asyncio.TimeoutError:
            partial_failures.append(
                {
                    "kind": "memory_op_rule_failed",
                    "rule_id": rule_id,
                    "error": "timeout",
                }
            )
            continue
        except Exception as exc:  # noqa: BLE001 — per-rule isolation
            logger.exception("memory_ops: rule %s raised", rule_id)
            partial_failures.append(
                {
                    "kind": "memory_op_rule_failed",
                    "rule_id": rule_id,
                    "error": str(exc)[:500],
                }
            )
            continue
        for draft in drafts:
            op = finalize_operation(
                draft=draft, run_id=run_id, cycle_key=cycle_key, now=now
            )
            all_operations.append(op)

    if include_cascade:
        try:
            from core.api.services.brain.cascade_rollup import build_cascade_drafts

            cascade_drafts = await build_cascade_drafts(
                snapshot=snapshot, now=now,
            )
            for draft in cascade_drafts:
                op = finalize_operation(
                    draft=draft, run_id=run_id, cycle_key=cycle_key, now=now
                )
                all_operations.append(op)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory_ops: cascade rollup failed")
            partial_failures.append(
                {
                    "kind": "memory_op_rule_failed",
                    "rule_id": "M8",
                    "error": str(exc)[:500],
                }
            )

    persisted, recurrence_keys = await _persist_operations(
        run_id=run_id, operations=all_operations
    )
    await _supersede_prior(run_id=run_id, recurrence_keys=recurrence_keys)

    return MemoryOpsReport(
        run_id=run_id,
        cycle_key=cycle_key,
        operation_count=persisted,
        partial_failures=partial_failures,
    )


# ---------------------------------------------------------------------------
# Read API — list / get / patch / apply
# ---------------------------------------------------------------------------


_PROPOSED_WRITE_BY_TYPE: dict[str, Any] = {}


def _load_proposed_write(target_type: str, raw_json: str | None) -> ProposedWrite:
    """Deserialize stored proposed_write payload into the right Pydantic model."""
    from core.api.models.brain import (
        ProposedWriteADR,
        ProposedWriteContextMdAppend,
        ProposedWriteDocPatch,
        ProposedWriteGuide,
        ProposedWriteKGEdgeMetric,
        ProposedWriteLearning,
        ProposedWriteNone,
        ProposedWriteTask,
    )

    data = json.loads(raw_json or "{}")
    if not isinstance(data, dict):
        data = {}
    data.setdefault("target_type", target_type)
    mapping: dict[str, Any] = {
        "none": ProposedWriteNone,
        "task": ProposedWriteTask,
        "guide": ProposedWriteGuide,
        "adr": ProposedWriteADR,
        "learning": ProposedWriteLearning,
        "kg_edge_metric": ProposedWriteKGEdgeMetric,
        "doc_patch": ProposedWriteDocPatch,
        "context_md_append": ProposedWriteContextMdAppend,
    }
    cls = mapping.get(target_type, ProposedWriteNone)
    try:
        return cls.model_validate(data)
    except Exception:
        return ProposedWriteNone()


def _row_to_operation(row: aiosqlite.Row | tuple, evidence: list[str]) -> MemoryOperation:
    get = (lambda key: row[key]) if hasattr(row, "keys") else None
    if get is None:
        raise TypeError("brain_memory_operations row missing keys access")
    direction = OPERATION_DIRECTION.get(get("operation_type"), "connect")
    score = float(get("score"))
    proposed = _load_proposed_write(
        get("proposed_write_target_type"), get("proposed_write_json")
    )
    return MemoryOperation(
        operation_id=get("operation_id"),
        run_id=get("run_id"),
        cycle_key=get("cycle_key"),
        detected_at=_parse_iso(get("detected_at")) or datetime.now(timezone.utc),
        operation_type=get("operation_type"),
        schema_version=get("schema_version"),
        scope_type=get("scope_type"),
        scope_key=get("scope_key"),
        program_key=get("program_key"),
        source_ref=get("source_ref"),
        target_ref=get("target_ref") or "",
        score=score,
        recurrence_key=get("recurrence_key"),
        recurrence_count=get("recurrence_count") or 1,
        first_seen_cycle_key=get("first_seen_cycle_key"),
        last_seen_cycle_key=get("last_seen_cycle_key"),
        involved_projects=_parse_list(get("involved_projects_json")),
        evidence=evidence,
        evidence_hash=get("evidence_hash"),
        summary=get("summary"),
        proposed_write=proposed,
        myelin_effect=MyelinEffect(direction=direction, score=score),
        requires_approval=True,
        approval_state=get("approval_state"),
        expires_at=_parse_iso(get("expires_at")) or datetime.now(timezone.utc),
        superseded_by_operation_id=get("superseded_by_operation_id"),
        applied_at=_parse_iso(get("applied_at")),
        applied_by_user_id=get("applied_by_user_id"),
        applied_artifact_ref=get("applied_artifact_ref"),
    )


def _redact(op: MemoryOperation) -> MemoryOperationRedacted:
    return MemoryOperationRedacted(
        operation_id=op.operation_id,
        cycle_key=op.cycle_key,
        operation_type=op.operation_type,
    )


def _is_visible(visible: set[str] | None, involved_projects: list[str]) -> bool:
    if visible is None:
        return True
    if not involved_projects:
        return True
    return set(involved_projects).issubset(visible)


_SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _encode_cursor(score: float, detected_at: str, operation_id: str) -> str:
    payload = json.dumps(
        {"sc": round(score, 6), "d": detected_at, "i": operation_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[float, str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        return (
            float(payload["sc"]),
            str(payload["d"]),
            str(payload["i"]),
        )
    except (KeyError, ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return None


async def _resolve_run(
    db: aiosqlite.Connection,
    *,
    cycle_key: str | None,
    run_id: str | None,
    workspace_id: str,
) -> dict[str, Any] | None:
    if run_id:
        row = await (
            await db.execute(
                "SELECT run_id, cycle_key FROM brain_runs WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "cycle_key": row[1]}
    if not cycle_key or cycle_key == "latest":
        row = await (
            await db.execute(
                "SELECT run_id, cycle_key FROM brain_runs"
                " WHERE workspace_id = ? AND status IN ('succeeded','partial')"
                "  AND superseded_by_run_id IS NULL"
                " ORDER BY cycle_key DESC, started_at DESC LIMIT 1",
                (workspace_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "cycle_key": row[1]}
    row = await (
        await db.execute(
            "SELECT run_id, cycle_key FROM brain_runs"
            " WHERE workspace_id = ? AND cycle_key = ?"
            "  AND status IN ('succeeded','partial')"
            "  AND superseded_by_run_id IS NULL"
            " ORDER BY started_at DESC LIMIT 1",
            (workspace_id, cycle_key),
        )
    ).fetchone()
    if row is None:
        return None
    return {"run_id": row[0], "cycle_key": row[1]}


async def _fetch_evidence(
    db: aiosqlite.Connection, operation_id: str
) -> list[str]:
    rows = await (
        await db.execute(
            "SELECT evidence_kind, evidence_ref, position"
            " FROM brain_memory_operation_evidence"
            " WHERE operation_id = ?"
            " ORDER BY position ASC",
            (operation_id,),
        )
    ).fetchall()
    evidence: list[str] = []
    kind_prefix = {
        "digest_event": "event",
        "drift_signal": "drift",
        "journal_entry": "journal",
        "handoff": "handoff",
        "learning": "learning",
        "kg_node": "kg",
        "task": "task",
        "pr": "pr",
        "commit": "commit",
        "audit_log": "audit",
    }
    for r in rows:
        get = (lambda key: r[key]) if hasattr(r, "keys") else None
        if get is not None:
            kind = get("evidence_kind")
            ref = get("evidence_ref")
        else:
            kind, ref, _ = r
        prefix = kind_prefix.get(kind, kind)
        evidence.append(f"{prefix}:{ref}")
    return evidence


async def list_memory_operations(
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    operation_types: list[str] | None = None,
    approval_states: list[str] | None = None,
    recurrence_min: int = 1,
    score_min: float = 0.0,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: UserInfo | None = None,
    workspace_id: str = "ws_default",
) -> MemoryOperationsListResponse:
    limit = max(1, min(MAX_LIMIT, int(limit)))
    over_fetch = limit + 1
    states_to_apply = approval_states or ["pending"]

    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        run = await _resolve_run(
            db, cycle_key=cycle_key, run_id=run_id, workspace_id=workspace_id
        )
        if run is None:
            return MemoryOperationsListResponse(items=[], total_returned=0)

        visible = await get_visible_projects(db, user, workspace_id) if user else None

        where = ["o.run_id = ?"]
        params: list[Any] = [run["run_id"]]
        if scope_type:
            where.append("o.scope_type = ?")
            params.append(scope_type)
        if scope_key:
            where.append("o.scope_key = ?")
            params.append(scope_key)
        if operation_types:
            placeholders = ",".join("?" for _ in operation_types)
            where.append(f"o.operation_type IN ({placeholders})")
            params.extend(operation_types)
        if states_to_apply:
            placeholders = ",".join("?" for _ in states_to_apply)
            where.append(f"o.approval_state IN ({placeholders})")
            params.extend(states_to_apply)
        if recurrence_min > 1:
            where.append("o.recurrence_count >= ?")
            params.append(recurrence_min)
        if score_min > 0:
            where.append("o.score >= ?")
            params.append(score_min)

        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded:
                cur_score, cur_dt, cur_id = decoded
                where.append("(o.score, o.detected_at, o.operation_id) < (?, ?, ?)")
                params.extend([cur_score, cur_dt, cur_id])

        query = (
            "SELECT o.* FROM brain_memory_operations o "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY o.score DESC, o.detected_at DESC, o.operation_id ASC "
            "LIMIT ?"
        )
        params.append(over_fetch)
        rows = await (await db.execute(query, params)).fetchall()

        items: list[MemoryOperation | MemoryOperationRedacted] = []
        next_cursor: str | None = None
        redacted_count = 0
        page_rows = rows[:limit]
        if len(rows) > limit:
            last = page_rows[-1]
            next_cursor = _encode_cursor(
                last["score"], last["detected_at"], last["operation_id"]
            )
        for row in page_rows:
            evidence = await _fetch_evidence(db, row["operation_id"])
            op = _row_to_operation(row, evidence)
            if not _is_visible(visible, op.involved_projects):
                redacted_count += 1
                items.append(_redact(op))
                continue
            items.append(op)

    return MemoryOperationsListResponse(
        items=items,
        next_cursor=next_cursor,
        cycle_key=run["cycle_key"],
        run_id=run["run_id"],
        redacted_count=redacted_count,
        total_returned=len(items),
    )


async def fetch_single_operation(
    *,
    operation_id: str,
    user: UserInfo | None,
    workspace_id: str = "ws_default",
) -> MemoryOperation | None:
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                "SELECT * FROM brain_memory_operations WHERE operation_id = ?",
                (operation_id,),
            )
        ).fetchone()
        if row is None:
            return None
        evidence = await _fetch_evidence(db, operation_id)
        visible = await get_visible_projects(db, user, workspace_id) if user else None
    op = _row_to_operation(row, evidence)
    if not _is_visible(visible, op.involved_projects):
        return None
    return op


_ALLOWED_TRANSITIONS: dict[ApprovalState, set[ApprovalState]] = {
    "pending": {"approved", "rejected", "dismissed", "expired", "superseded"},
    "approved": {"applied"},
    "applied": {"reverted"},
    "rejected": set(),
    "dismissed": set(),
    "superseded": set(),
    "expired": set(),
    "reverted": set(),
}


def _action_to_state(action: str) -> ApprovalState | None:
    mapping = {
        "approve": "approved",
        "approved": "approved",
        "dismiss": "dismissed",
        "dismissed": "dismissed",
        "reject": "rejected",
        "rejected": "rejected",
    }
    return mapping.get(action)  # type: ignore[return-value]


class LifecycleConflict(Exception):
    """Raised when target state is invalid from current state."""

    def __init__(self, current: str, attempted: str):
        super().__init__(f"lifecycle: {current} → {attempted} forbidden")
        self.current = current
        self.attempted = attempted


async def apply_lifecycle_patch(
    *,
    operation_id: str,
    action: str,
    reason: str | None,
    applied_artifact_ref: str | None,
    user: UserInfo,
    workspace_id: str = "ws_default",
    now: datetime,
    idempotency_key: str | None = None,
) -> MemoryOperation | None:
    """PATCH lifecycle transition. Idempotent on same target state."""
    target_state = _action_to_state(action)
    if target_state is None:
        raise LifecycleConflict(current="?", attempted=action)
    existing = await fetch_single_operation(
        operation_id=operation_id, user=user, workspace_id=workspace_id
    )
    if existing is None:
        return None
    current = existing.approval_state
    if current == target_state:
        # Idempotent — same target → 200, no rewrite.
        return existing
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if target_state not in allowed:
        raise LifecycleConflict(current=current, attempted=target_state)

    iso_now = _utc_iso(now)
    async with write_db() as db:
        if target_state == "applied":
            await db.execute(
                "UPDATE brain_memory_operations SET"
                "  approval_state = ?, applied_at = ?, applied_by_user_id = ?,"
                "  applied_artifact_ref = ?"
                " WHERE operation_id = ?",
                (
                    target_state,
                    iso_now,
                    user.user_id,
                    applied_artifact_ref,
                    operation_id,
                ),
            )
        else:
            await db.execute(
                "UPDATE brain_memory_operations SET approval_state = ?,"
                "  applied_artifact_ref = COALESCE(?, applied_artifact_ref)"
                " WHERE operation_id = ?",
                (target_state, applied_artifact_ref, operation_id),
            )
        await db.execute(
            "INSERT INTO brain_memory_operation_states ("
            " state_id, operation_id, from_state, to_state, actor_user_id,"
            " reason, applied_artifact_ref"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                operation_id,
                current,
                target_state,
                user.user_id,
                reason,
                applied_artifact_ref,
            ),
        )
    return await fetch_single_operation(
        operation_id=operation_id, user=user, workspace_id=workspace_id
    )


# ---------------------------------------------------------------------------
# Apply guidance (NO writes — returns next_action only)
# ---------------------------------------------------------------------------


def _next_action_for(op: MemoryOperation) -> ApplyNextAction:
    """Server-side mapping table (sub-03 §11.1 apply guidance)."""
    must_include = f"brain_op:{op.operation_id}"
    args: dict[str, Any] = {}
    target_path: str | None = None
    body: str | None = None
    tool: str | None = None
    rationale = ""
    pw = op.proposed_write
    target_type = pw.target_type  # type: ignore[union-attr]

    if op.operation_type == "reinforce":
        if target_type == "none":
            # Fase D temporal recency: bump the node's last_verified_at via the
            # write-path tool (proposed_write is `none`; the node id rides in
            # source_ref). Guidance-only: the operator/agent runs the tool.
            tool = "mcp__marvis__mark_kg_verified"
            args = {"node_id": op.source_ref}
            rationale = (
                "Temporal recency: this live node is aging and never verified. "
                "Approve → mark_kg_verified stamps last_verified_at so it reads fresh."
            )
        else:
            tool = None
            rationale = "reinforce produces edge metric evidence only; v2 may write to graph_edge_metrics."
            if hasattr(pw, "edge_id"):
                args = {"edge_id": pw.edge_id, "metric_kind": pw.metric_kind, "delta": pw.delta}
    elif op.operation_type == "cascade_rollup":
        tool = None
        rationale = "Cascade rollup is apply-guidance-only. Operator appends body to parent context.md."
        if hasattr(pw, "path"):
            target_path = pw.path
            body = getattr(pw, "body", None)
    elif op.operation_type == "orphan_detected":
        tool = "mcp__marvis__create_task"
        rationale = "orphan_detected → triage task tagged brain_op:{id}."
        if hasattr(pw, "title"):
            args = {
                "title": pw.title,
                "description": pw.description,
                "project": pw.project,
                "priority": pw.priority,
                "delegation": pw.delegation,
                "impact": pw.impact,
                "confidence": pw.confidence,
                "ease": pw.ease,
                "tags": list(pw.tags),
            }
    elif op.operation_type == "provenance_hardening":
        tool = "mcp__marvis__create_task"
        rationale = "provenance_hardening → task to attach proper source chain."
        if hasattr(pw, "title"):
            args = {
                "title": pw.title,
                "description": pw.description,
                "project": pw.project,
                "priority": pw.priority,
                "delegation": pw.delegation,
                "impact": pw.impact,
                "confidence": pw.confidence,
                "ease": pw.ease,
                "tags": list(pw.tags),
            }
    elif op.operation_type == "contradiction_detected":
        tool = "mcp__marvis__create_task"
        rationale = "contradiction_detected → resolution task."
        if hasattr(pw, "title"):
            args = {
                "title": pw.title,
                "description": pw.description,
                "project": pw.project,
                "priority": pw.priority,
                "delegation": pw.delegation,
                "impact": pw.impact,
                "confidence": pw.confidence,
                "ease": pw.ease,
                "tags": list(pw.tags),
            }
    elif op.operation_type in ("consolidate", "supersede_candidate"):
        tool = None
        rationale = (
            f"{op.operation_type}: manual Edit. Returns target path + rationale "
            "for operator to perform the merge."
        )
        if hasattr(pw, "path"):
            target_path = pw.path
            body = getattr(pw, "rationale", None)
    elif op.operation_type in ("compression_candidate", "promotion_candidate"):
        tool = "mcp__marvis__create_learning"
        rationale = "Promote stable pattern into a learning artifact."
        if hasattr(pw, "title"):
            args = {
                "title": pw.title,
                "category": pw.category,
                "description": pw.description,
                "prevention": pw.prevention,
                "severity": pw.severity,
                "module": pw.module,
                "project": pw.project,
                "tags": list(pw.tags),
            }
    else:
        tool = None
        rationale = "No canonical apply path; operator inspects evidence and decides."

    return ApplyNextAction(
        tool=tool,
        args=args,
        rationale=rationale,
        must_include_in_tags=must_include,
        target_path=target_path,
        body=body,
    )


class ApplyPreconditionError(Exception):
    """Raised when the operation is not in a state that supports apply."""

    def __init__(self, kind: str, current_state: str):
        super().__init__(f"apply precondition: {kind} (state={current_state})")
        self.kind = kind
        self.current_state = current_state


async def get_apply_guidance(
    *,
    operation_id: str,
    user: UserInfo,
    workspace_id: str = "ws_default",
) -> ApplyResponse | None:
    op = await fetch_single_operation(
        operation_id=operation_id, user=user, workspace_id=workspace_id
    )
    if op is None:
        return None
    if op.approval_state != "approved":
        raise ApplyPreconditionError("not_approved", op.approval_state)
    if op.applied_artifact_ref:
        raise ApplyPreconditionError("already_applied", op.approval_state)
    next_action = _next_action_for(op)
    return ApplyResponse(
        operation_id=op.operation_id,
        next_action=next_action,
        operation_summary={
            "operation_type": op.operation_type,
            "proposed_write_summary": op.summary,
            "myelin_direction": op.myelin_effect.direction,
            "score": op.myelin_effect.score,
            "scope_type": op.scope_type,
            "scope_key": op.scope_key,
        },
    )


__all__ = [
    "ApplyPreconditionError",
    "BULK_PATCH_MAX",
    "DEFAULT_LIMIT",
    "DEFAULT_RULE_TIMEOUT_S",
    "EXPIRY_DAYS_DEFAULT",
    "LifecycleConflict",
    "MAX_LIMIT",
    "MemoryOpsReport",
    "OPERATION_DIRECTION",
    "OpSnapshot",
    "OperationDraft",
    "REGISTERED_RULES",
    "apply_lifecycle_patch",
    "build_snapshot",
    "canonical_evidence",
    "evidence_hash",
    "fetch_single_operation",
    "finalize_operation",
    "get_apply_guidance",
    "list_memory_operations",
    "make_operation_id",
    "make_recurrence_key",
    "run_phase",
]
