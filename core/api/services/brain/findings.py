# Brain v1 — Learn Findings service (sub-04 L5 — §4 / §5 / §10).
#
# Findings is the FINAL phase of brain_runs (after Memory-Ops). The
# orchestrator:
#   1. Builds a FindingSnapshot (read-only projection — digest events +
#      journal entries + drift signals + memory operations of the current
#      run_id).
#   2. Invokes each F-rule (F1-F6) with a 15s timeout; per-rule failures
#      isolated and reported via partial_failures_json.
#   3. Persists findings via INSERT OR IGNORE (BLAKE2b stable id).
#   4. Updates supersede chain across prior open findings sharing the same
#      proposal_fingerprint AND bumps recurrence_count on continuing rows.
#
# Layering invariants (parent §9, sub-04 §7 / §10.Z):
#   * NO LLM imports (parent §9.3). AST-grep test enforces.
#   * NO raw SQL on substrate (tasks/PR/handoffs/learnings/kg_edges).
#   * NO mutation of substrate from this module.
#   * Findings NEVER re-reads substrate — only L2 events + journal entries
#     + L3 drift signals + L4 memory operations scoped to the current
#     run_id (sub-04 §10.Y — same cycle envelope).
#   * Stable BLAKE2b 16-byte finding_id. EXCLUDES: severity, confidence,
#     summary, title, approval_state, owner_hint, suggested_artifact
#     (sub-04 §7.2).
#   * confidence is a CATEGORICAL TIER (low|medium|high), NEVER float.
#   * Apply guidance ONLY — no auto-write to substrate (sub-04 F1 binding).
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import aiosqlite

from core.api.db import acquire_db, write_db
from core.api.models.brain import (
    ArtifactSelector,
    ClosureArtifactExists,
    ClosureCondition,
    ClosureDriftSignalClears,
    ClosureManualAttest,
    ClosureMemoryOpApplied,
    ConfidenceTier,
    Finding,
    FindingApprovalState,
    FindingType,
    OwnerHint,
    ScopeTypeL4,
    Severity,
    SuggestedArtifact,
)
from core.api.services.brain.owner_hint import compute_owner_hint

logger = logging.getLogger(__name__)

DEFAULT_RULE_TIMEOUT_S = 15
DEFAULT_OPEN_TTL_DAYS = 60
PER_TYPE_CAP_DEFAULT = 100

_SEVERITY_RANK_INT: dict[Severity, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

_CONFIDENCE_RANK_INT: dict[ConfidenceTier, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


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


def _canonical_evidence(evidence: Iterable[str]) -> str:
    """Stable JSON for hash. Mirror sub-02 / sub-03 helper (intentionally
    duplicated to avoid importing a private helper across layers)."""
    norm = sorted(str(item) for item in evidence)
    return json.dumps(norm, sort_keys=False, ensure_ascii=False, separators=(",", ":"))


def evidence_hash(evidence: Iterable[str]) -> str:
    """sha256 64-char hex per sub-04 §7 contract."""
    return hashlib.sha256(_canonical_evidence(evidence).encode("utf-8")).hexdigest()


def _closure_condition_hash(closure: ClosureCondition) -> str:
    """BLAKE2b-8 of canonical JSON serialization (kind + sorted params).

    Used as a salt in the finding_id payload so two findings with identical
    natural keys but different closure conditions get distinct stable IDs.
    """
    if hasattr(closure, "model_dump"):
        payload = closure.model_dump(mode="json")
    else:
        payload = closure
    return hashlib.blake2b(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        digest_size=8,
    ).hexdigest()


def make_finding_id(
    *,
    cycle_key: str,
    finding_type: FindingType,
    scope_type: ScopeTypeL4,
    scope_key: str,
    evidence_source_refs_sorted: list[str],
    closure_condition_hash_hex: str,
) -> str:
    """Stable BLAKE2b-16 hex (sub-04 §7.2).

    EXCLUDES: severity, confidence, summary, title, approval_state,
    owner_hint, suggested_artifact. Recompute idempotent across interpreter
    runs given identical natural-key inputs.
    """
    payload = (
        f"{cycle_key}|{finding_type}|{scope_type}|{scope_key}|"
        f"{'|'.join(evidence_source_refs_sorted)}|{closure_condition_hash_hex}"
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def make_proposal_fingerprint(
    *,
    finding_type: FindingType,
    scope_type: ScopeTypeL4,
    scope_key: str,
    evidence_source_refs_sorted: list[str],
    closure_condition_hash_hex: str,
) -> str:
    """BLAKE2b-16 hex without cycle_key — groups same finding across cycles."""
    payload = (
        f"{finding_type}|{scope_type}|{scope_key}|"
        f"{'|'.join(evidence_source_refs_sorted)}|{closure_condition_hash_hex}"
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


# ---------------------------------------------------------------------------
# Snapshot (read-only projection consumed by all F-rules)
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
class MemoryOpRow:
    operation_id: str
    cycle_key: str
    operation_type: str
    scope_type: str
    scope_key: str
    program_key: str | None
    source_ref: str
    target_ref: str
    approval_state: str
    score: float
    recurrence_count: int
    involved_projects: list[str]


@dataclass(slots=True, frozen=True)
class FindingSnapshot:
    """Read-only L2/L3/L4 projection consumed by all F-rules."""

    cycle_key: str
    run_id: str
    workspace_id: str
    as_of: datetime
    events: tuple[DigestEventRow, ...]
    journal_entries: tuple[JournalEntryRow, ...]
    drift_signals: tuple[DriftSignalRow, ...]
    memory_ops: tuple[MemoryOpRow, ...]


@dataclass(slots=True)
class FindingsReport:
    """Return envelope for findings.run_phase()."""

    run_id: str
    cycle_key: str
    finding_count: int = 0
    partial_failures: list[dict[str, str]] = field(default_factory=list)


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _parse_json_list(raw: str | None) -> list[Any]:
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
) -> FindingSnapshot:
    """Read-only projection: digest events + journal entries + drift signals
    + memory operations for the current run_id. Findings NEVER re-reads
    substrate (sub-04 §10.Y / §10.Z invariant 4).
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
        mo_rows = await (
            await db.execute(
                "SELECT operation_id, cycle_key, operation_type, scope_type, scope_key,"
                " program_key, source_ref, target_ref, approval_state, score,"
                " recurrence_count, involved_projects_json"
                " FROM brain_memory_operations WHERE run_id = ?",
                (run_id,),
            )
        ).fetchall()

    events = tuple(
        DigestEventRow(
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
            observed_at=_parse_iso(r["observed_at"]) or now,
            evidence=_parse_json_dict(r["evidence_json"]),
        )
        for r in ev_rows
    )
    journals = tuple(
        JournalEntryRow(
            entry_id=r["entry_id"],
            cycle_key=r["cycle_key"],
            scope_type=r["scope_type"],
            scope_key=r["scope_key"],
            program_key=r["program_key"],
            body=_parse_json_dict(r["body_json"]),
            is_empty=bool(r["is_empty"]),
            published_at=_parse_iso(r["published_at"]) or now,
        )
        for r in jn_rows
    )
    signals = tuple(
        DriftSignalRow(
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
            confidence=float(r["confidence"] or 0.0),
            recurrence_key=r["recurrence_key"],
            involved_projects=[
                str(p) for p in _parse_json_list(r["involved_projects_json"])
            ],
        )
        for r in dr_rows
    )
    memory_ops = tuple(
        MemoryOpRow(
            operation_id=r["operation_id"],
            cycle_key=r["cycle_key"],
            operation_type=r["operation_type"],
            scope_type=r["scope_type"],
            scope_key=r["scope_key"],
            program_key=r["program_key"],
            source_ref=r["source_ref"],
            target_ref=r["target_ref"] or "",
            approval_state=r["approval_state"],
            score=float(r["score"] or 0.0),
            recurrence_count=int(r["recurrence_count"] or 1),
            involved_projects=[
                str(p) for p in _parse_json_list(r["involved_projects_json"])
            ],
        )
        for r in mo_rows
    )

    return FindingSnapshot(
        cycle_key=cycle_key,
        run_id=run_id,
        workspace_id=workspace_id,
        as_of=now,
        events=events,
        journal_entries=journals,
        drift_signals=signals,
        memory_ops=memory_ops,
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FindingDraft:
    """Raw rule output before stable-id derivation + persistence."""

    finding_type: FindingType
    scope_type: ScopeTypeL4
    scope_key: str
    program_key: str | None
    title: str
    summary: str
    why_now: str
    evidence: list[str]
    suggested_artifact: SuggestedArtifact
    closure_condition: ClosureCondition
    severity: Severity
    confidence: ConfidenceTier
    involved_projects: list[str] = field(default_factory=list)
    owner_hint: OwnerHint | None = None
    closure_condition_human: str | None = None


def _derive_confidence(
    *,
    drift_refs: int,
    memory_op_refs: int,
    recurrence_count: int,
) -> ConfidenceTier:
    """Sub-04 §4 / §7.4 prose:
      High: deterministic evidence (drift + memory op) + recurrence >= 3.
      Medium: deterministic drift OR memory op + single-cycle evidence.
      Low: weak/noisy source (no drift, no memory op).

    Drift signals and memory ops are BOTH "deterministic" evidence sources
    (both are produced by deterministic rules upstream of Findings). Recurrence
    over 3 cycles bumps to high regardless of which path supplied the signal.
    """
    deterministic = drift_refs + memory_op_refs
    if deterministic >= 1 and recurrence_count >= 3:
        return "high"
    if deterministic >= 1:
        return "medium"
    return "low"


async def finalize_finding(
    *,
    draft: FindingDraft,
    run_id: str,
    cycle_key: str,
    now: datetime,
    recurrence_count: int = 1,
    first_seen_cycle_key: str | None = None,
    open_ttl_days: int = DEFAULT_OPEN_TTL_DAYS,
    db: aiosqlite.Connection | None = None,
) -> Finding:
    """Derive id/hash/owner_hint and construct a Finding Pydantic instance.

    `db` is optional — if supplied, the owner_hint lookup reuses the
    existing connection; otherwise a short-lived read pool connection is
    acquired. Owner hint failure degrades to None (sub-04 §7.5).
    """
    evidence_sorted = sorted(set(draft.evidence))
    ev_hash = evidence_hash(evidence_sorted)
    cc_hash = _closure_condition_hash(draft.closure_condition)
    finding_id = make_finding_id(
        cycle_key=cycle_key,
        finding_type=draft.finding_type,
        scope_type=draft.scope_type,
        scope_key=draft.scope_key,
        evidence_source_refs_sorted=evidence_sorted,
        closure_condition_hash_hex=cc_hash,
    )
    proposal_fp = make_proposal_fingerprint(
        finding_type=draft.finding_type,
        scope_type=draft.scope_type,
        scope_key=draft.scope_key,
        evidence_source_refs_sorted=evidence_sorted,
        closure_condition_hash_hex=cc_hash,
    )

    owner_hint = draft.owner_hint
    if owner_hint is None:
        try:
            owner_hint = await compute_owner_hint(
                scope_type=draft.scope_type, scope_key=draft.scope_key, db=db
            )
        except Exception:
            owner_hint = None

    return Finding(
        finding_id=finding_id,
        run_id=run_id,
        cycle_key=cycle_key,
        detected_at=now.astimezone(timezone.utc),
        finding_type=draft.finding_type,
        schema_version=1,
        scope_type=draft.scope_type,
        scope_key=draft.scope_key,
        program_key=draft.program_key,
        title=draft.title[:200],
        summary=draft.summary[:2000],
        why_now=draft.why_now[:500],
        evidence=evidence_sorted,
        evidence_hash=ev_hash,
        involved_projects=sorted(set(draft.involved_projects)),
        suggested_artifact=draft.suggested_artifact,
        owner_hint=owner_hint,
        closure_condition=draft.closure_condition,
        closure_condition_human=draft.closure_condition_human,
        severity=draft.severity,
        confidence=draft.confidence,
        approval_state="open",
        regression_of_finding_id=None,
        proposal_fingerprint=proposal_fp,
        recurrence_count=recurrence_count,
        first_seen_cycle_key=first_seen_cycle_key or cycle_key,
        last_seen_cycle_key=cycle_key,
        expires_at=(now + timedelta(days=open_ttl_days)).astimezone(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Rule builders — F1-F6 (sub-04 §3 mapping rules)
# ---------------------------------------------------------------------------
#
# Each rule consumes ONLY the snapshot. Drift signals are the dominant
# producer (5/6 mappings); memory operations contribute through F6
# promotion_candidate. F-rules NEVER touch substrate (parent §9 invariant).


def _project_scope(slug: str | None) -> tuple[ScopeTypeL4, str]:
    return ("project", slug) if slug else ("company", "__company__")


def _coerce_scope_type(value: str) -> ScopeTypeL4:
    if value in ("company", "program", "project", "artifact"):
        return value  # type: ignore[return-value]
    return "company"


def _drift_signal_evidence(signal: DriftSignalRow) -> list[str]:
    return [f"drift_signal:{signal.signal_id}"]


def _memory_op_evidence(op: MemoryOpRow) -> list[str]:
    return [f"memory_op:{op.operation_id}"]


def _truncate(s: str, *, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


async def _f1_decision_without_adr(
    snapshot: FindingSnapshot, *, run_id: str, now: datetime
) -> list[FindingDraft]:
    """DR2 decision_without_adr → open_question OR task_candidate (sub-04 §3)."""
    drafts: list[FindingDraft] = []
    for sig in snapshot.drift_signals:
        if sig.signal_type != "decision_without_adr":
            continue
        scope_type = _coerce_scope_type(sig.scope_type)
        scope_key = sig.scope_key if scope_type != "company" else "__company__"
        evidence = _drift_signal_evidence(sig)
        severity: Severity = sig.severity if sig.severity in _SEVERITY_RANK_INT else "medium"  # type: ignore[assignment]
        confidence = _derive_confidence(drift_refs=1, memory_op_refs=0, recurrence_count=1)
        is_task = sig.severity in ("high", "critical")
        finding_type: FindingType = "task_candidate" if is_task else "open_question"
        suggested: SuggestedArtifact = "adr"
        title = _truncate(
            f"Decision without ADR on {sig.observed_direction_ref}",
            limit=200,
        )
        summary = _truncate(
            f"DR2 {sig.signal_id}: scope={scope_type}/{scope_key} flags "
            f"{sig.observed_direction_ref} as decision_without_adr "
            f"(knowledge_form={sig.knowledge_form}).",
            limit=2000,
        )
        why_now = _truncate(
            f"Drift rule DR2 fired this cycle for {sig.observed_direction_ref}.",
            limit=500,
        )
        closure: ClosureCondition = ClosureArtifactExists(
            artifact_kind="adr",
            selector=ArtifactSelector(
                tag_match="brain_finding:{finding_id}",
            ),
        )
        drafts.append(
            FindingDraft(
                finding_type=finding_type,
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=sig.program_key,
                title=title,
                summary=summary,
                why_now=why_now,
                evidence=evidence,
                suggested_artifact=suggested,
                closure_condition=closure,
                severity=severity,
                confidence=confidence,
                involved_projects=list(sig.involved_projects),
            )
        )
    return drafts


async def _f2_playbook_changed(
    snapshot: FindingSnapshot, *, run_id: str, now: datetime
) -> list[FindingDraft]:
    """playbook_changed drift → procedure_change finding (sub-04 §3)."""
    drafts: list[FindingDraft] = []
    for sig in snapshot.drift_signals:
        if sig.signal_type != "playbook_changed":
            continue
        scope_type = _coerce_scope_type(sig.scope_type)
        scope_key = sig.scope_key if scope_type != "company" else "__company__"
        severity: Severity = sig.severity if sig.severity in _SEVERITY_RANK_INT else "medium"  # type: ignore[assignment]
        evidence = _drift_signal_evidence(sig)
        title = _truncate(
            f"Playbook change observed for {sig.observed_direction_ref}",
            limit=200,
        )
        summary = _truncate(
            f"DR3 / playbook_changed: {sig.observed_direction_ref} "
            f"(knowledge_form={sig.knowledge_form}) changed without a guide update.",
            limit=2000,
        )
        why_now = _truncate(
            f"Drift signal {sig.signal_id} flags playbook drift this cycle.",
            limit=500,
        )
        closure: ClosureCondition = ClosureArtifactExists(
            artifact_kind="guide",
            selector=ArtifactSelector(
                tag_match="brain_finding:{finding_id}",
            ),
        )
        drafts.append(
            FindingDraft(
                finding_type="procedure_change",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=sig.program_key,
                title=title,
                summary=summary,
                why_now=why_now,
                evidence=evidence,
                suggested_artifact="guide",
                closure_condition=closure,
                severity=severity,
                confidence=_derive_confidence(
                    drift_refs=1, memory_op_refs=0, recurrence_count=1
                ),
                involved_projects=list(sig.involved_projects),
            )
        )
    return drafts


async def _f3_stale_open_loop(
    snapshot: FindingSnapshot, *, run_id: str, now: datetime
) -> list[FindingDraft]:
    """stale_open_loop drift → task_candidate (sub-04 §3)."""
    drafts: list[FindingDraft] = []
    for sig in snapshot.drift_signals:
        if sig.signal_type != "stale_open_loop":
            continue
        scope_type = _coerce_scope_type(sig.scope_type)
        scope_key = sig.scope_key if scope_type != "company" else "__company__"
        severity: Severity = sig.severity if sig.severity in _SEVERITY_RANK_INT else "medium"  # type: ignore[assignment]
        evidence = _drift_signal_evidence(sig)
        title = _truncate(
            f"Stale open loop on {sig.observed_direction_ref}",
            limit=200,
        )
        summary = _truncate(
            f"DR4 / stale_open_loop: {sig.observed_direction_ref} has had no "
            f"observable progress (knowledge_form={sig.knowledge_form}).",
            limit=2000,
        )
        why_now = _truncate(
            f"Drift signal {sig.signal_id} flags stale loop this cycle.",
            limit=500,
        )
        closure: ClosureCondition = ClosureDriftSignalClears(
            drift_signal_id=sig.signal_id,
            consecutive_clear_cycles=2,
        )
        drafts.append(
            FindingDraft(
                finding_type="task_candidate",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=sig.program_key,
                title=title,
                summary=summary,
                why_now=why_now,
                evidence=evidence,
                suggested_artifact="task",
                closure_condition=closure,
                severity=severity,
                confidence=_derive_confidence(
                    drift_refs=1, memory_op_refs=0, recurrence_count=1
                ),
                involved_projects=list(sig.involved_projects),
            )
        )
    return drafts


async def _f4_external_update_unpropagated(
    snapshot: FindingSnapshot, *, run_id: str, now: datetime
) -> list[FindingDraft]:
    """external_update_unpropagated → task_candidate (sub-04 §3)."""
    drafts: list[FindingDraft] = []
    for sig in snapshot.drift_signals:
        if sig.signal_type != "external_update_unpropagated":
            continue
        scope_type = _coerce_scope_type(sig.scope_type)
        scope_key = sig.scope_key if scope_type != "company" else "__company__"
        severity: Severity = sig.severity if sig.severity in _SEVERITY_RANK_INT else "medium"  # type: ignore[assignment]
        evidence = _drift_signal_evidence(sig)
        title = _truncate(
            f"External update needs propagation: {sig.observed_direction_ref}",
            limit=200,
        )
        summary = _truncate(
            f"DR6 / external_update_unpropagated: {sig.observed_direction_ref} "
            "carries upstream changes not yet reflected in our context.",
            limit=2000,
        )
        why_now = _truncate(
            f"Drift signal {sig.signal_id} flags unpropagated update this cycle.",
            limit=500,
        )
        closure: ClosureCondition = ClosureArtifactExists(
            artifact_kind="task",
            selector=ArtifactSelector(
                tag_match="brain_finding:{finding_id}",
            ),
        )
        drafts.append(
            FindingDraft(
                finding_type="task_candidate",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=sig.program_key,
                title=title,
                summary=summary,
                why_now=why_now,
                evidence=evidence,
                suggested_artifact="task",
                closure_condition=closure,
                severity=severity,
                confidence=_derive_confidence(
                    drift_refs=1, memory_op_refs=0, recurrence_count=1
                ),
                involved_projects=list(sig.involved_projects),
            )
        )
    return drafts


async def _f5_claimed_decision_gap(
    snapshot: FindingSnapshot, *, run_id: str, now: datetime
) -> list[FindingDraft]:
    """claimed_decision_gap → scope_gap (sub-04 §3)."""
    drafts: list[FindingDraft] = []
    for sig in snapshot.drift_signals:
        if sig.signal_type != "claimed_decision_gap":
            continue
        scope_type = _coerce_scope_type(sig.scope_type)
        scope_key = sig.scope_key if scope_type != "company" else "__company__"
        severity: Severity = sig.severity if sig.severity in _SEVERITY_RANK_INT else "medium"  # type: ignore[assignment]
        evidence = _drift_signal_evidence(sig)
        title = _truncate(
            f"Claimed decision lacks evidence: {sig.observed_direction_ref}",
            limit=200,
        )
        summary = _truncate(
            f"DR7 / claimed_decision_gap: {sig.observed_direction_ref} surfaces "
            "as a claimed decision without the substrate to back it.",
            limit=2000,
        )
        why_now = _truncate(
            f"Drift signal {sig.signal_id} flags decision gap this cycle.",
            limit=500,
        )
        closure: ClosureCondition = ClosureManualAttest(
            instruction="Audit follow-up: verify the claimed decision and record evidence.",
        )
        drafts.append(
            FindingDraft(
                finding_type="scope_gap",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=sig.program_key,
                title=title,
                summary=summary,
                why_now=why_now,
                evidence=evidence,
                suggested_artifact="status_update",
                closure_condition=closure,
                severity=severity,
                confidence=_derive_confidence(
                    drift_refs=1, memory_op_refs=0, recurrence_count=1
                ),
                involved_projects=list(sig.involved_projects),
            )
        )
    return drafts


async def _f6_contradiction(
    snapshot: FindingSnapshot, *, run_id: str, now: datetime
) -> list[FindingDraft]:
    """contradiction_detected memory ops → contradiction finding (sub-04 §3).

    Sources:
      * Memory ops with operation_type='contradiction_detected' (M7 output).
      * Promotion candidates (M-rule promotion_candidate) emit idea findings —
        handled inline here as the secondary trigger so we keep F-count to 6.
    """
    drafts: list[FindingDraft] = []
    for op in snapshot.memory_ops:
        if op.operation_type == "contradiction_detected":
            scope_type = _coerce_scope_type(op.scope_type)
            scope_key = op.scope_key if scope_type != "company" else "__company__"
            evidence = _memory_op_evidence(op)
            title = _truncate(
                f"Contradiction between {op.source_ref} and {op.target_ref}",
                limit=200,
            )
            summary = _truncate(
                f"Memory op {op.operation_id} surfaces two refs in tension. "
                f"Manual resolution required.",
                limit=2000,
            )
            why_now = _truncate(
                f"Memory operation {op.operation_id} fired this cycle.",
                limit=500,
            )
            closure: ClosureCondition = ClosureMemoryOpApplied(
                memory_operation_id=op.operation_id,
            )
            drafts.append(
                FindingDraft(
                    finding_type="contradiction",
                    scope_type=scope_type,
                    scope_key=scope_key,
                    program_key=op.program_key,
                    title=title,
                    summary=summary,
                    why_now=why_now,
                    evidence=evidence,
                    suggested_artifact="task",
                    closure_condition=closure,
                    severity="high",
                    confidence=_derive_confidence(
                        drift_refs=0,
                        memory_op_refs=1,
                        recurrence_count=op.recurrence_count,
                    ),
                    involved_projects=list(op.involved_projects),
                )
            )
            continue
        if op.operation_type == "promotion_candidate":
            scope_type = _coerce_scope_type(op.scope_type)
            scope_key = op.scope_key if scope_type != "company" else "__company__"
            evidence = _memory_op_evidence(op)
            title = _truncate(
                f"Promotion candidate: {op.source_ref}",
                limit=200,
            )
            summary = _truncate(
                f"Memory op {op.operation_id} signals {op.source_ref} as a "
                "stable pattern worth promoting to a learning or guide.",
                limit=2000,
            )
            why_now = _truncate(
                f"Memory operation {op.operation_id} reached promotion threshold this cycle.",
                limit=500,
            )
            closure_artifact: ClosureCondition = ClosureArtifactExists(
                artifact_kind="learning",
                selector=ArtifactSelector(
                    tag_match="brain_finding:{finding_id}",
                ),
            )
            drafts.append(
                FindingDraft(
                    finding_type="idea",
                    scope_type=scope_type,
                    scope_key=scope_key,
                    program_key=op.program_key,
                    title=title,
                    summary=summary,
                    why_now=why_now,
                    evidence=evidence,
                    suggested_artifact="learning",
                    closure_condition=closure_artifact,
                    severity="low",
                    confidence=_derive_confidence(
                        drift_refs=0,
                        memory_op_refs=1,
                        recurrence_count=op.recurrence_count,
                    ),
                    involved_projects=list(op.involved_projects),
                )
            )
    return drafts


REGISTERED_RULES: tuple[tuple[str, Any], ...] = (
    ("F1", _f1_decision_without_adr),
    ("F2", _f2_playbook_changed),
    ("F3", _f3_stale_open_loop),
    ("F4", _f4_external_update_unpropagated),
    ("F5", _f5_claimed_decision_gap),
    ("F6", _f6_contradiction),
)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _serialize_owner_hint(owner_hint: OwnerHint | None) -> str:
    if owner_hint is None:
        return "{}"
    return owner_hint.model_dump_json()


def _serialize_closure(closure: ClosureCondition) -> tuple[str, str]:
    """Return (kind, json_str)."""
    if hasattr(closure, "model_dump_json"):
        return (closure.kind, closure.model_dump_json())  # type: ignore[union-attr]
    raise TypeError(f"closure must be a Pydantic model, got {type(closure)!r}")


def _evidence_kind_for(ref: str) -> tuple[str, str]:
    if ":" in ref:
        prefix, rest = ref.split(":", 1)
    else:
        prefix, rest = "kg_node", ref
    mapping = {
        "event": "digest_event",
        "digest_event": "digest_event",
        "drift": "drift_signal",
        "drift_signal": "drift_signal",
        "journal": "journal_entry",
        "journal_entry": "journal_entry",
        "memory_op": "memory_op",
        "handoff": "handoff",
        "learning": "learning",
        "kg": "kg_node",
        "kg_node": "kg_node",
        "audit": "audit_log",
        "audit_log": "audit_log",
        "task": "task",
        "pr": "pr",
        "commit": "commit",
    }
    return (mapping.get(prefix, "kg_node"), rest)


async def _fetch_existing_finding(
    db: aiosqlite.Connection, finding_id: str
) -> aiosqlite.Row | None:
    return await (
        await db.execute(
            "SELECT finding_id, recurrence_count, first_seen_cycle_key,"
            " approval_state FROM brain_findings WHERE finding_id = ?",
            (finding_id,),
        )
    ).fetchone()


async def _persist_findings(
    *, run_id: str, findings: list[Finding]
) -> tuple[int, list[str]]:
    if not findings:
        return (0, [])
    persisted = 0
    fingerprints: list[str] = []
    async with write_db() as db:
        for f in findings:
            existing = await _fetch_existing_finding(db, f.finding_id)
            closure_kind, closure_json = _serialize_closure(f.closure_condition)
            if existing is not None:
                old_state = existing["approval_state"] if hasattr(existing, "keys") else existing[3]
                if old_state == "open":
                    await db.execute(
                        "UPDATE brain_findings SET"
                        "  last_seen_cycle_key = ?,"
                        "  recurrence_count = recurrence_count + 1"
                        " WHERE finding_id = ?",
                        (f.cycle_key, f.finding_id),
                    )
                continue
            await db.execute(
                "INSERT INTO brain_findings ("
                " finding_id, run_id, cycle_key, detected_at, finding_type,"
                " schema_version, scope_type, scope_key, program_key,"
                " title, summary, why_now, evidence_hash, involved_projects_json,"
                " suggested_artifact, owner_hint_json, closure_condition_kind,"
                " closure_condition_json, closure_condition_human,"
                " severity, confidence, approval_state, regression_of_finding_id,"
                " proposal_fingerprint, recurrence_count, first_seen_cycle_key,"
                " last_seen_cycle_key, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (
                    f.finding_id,
                    f.run_id,
                    f.cycle_key,
                    _utc_iso(f.detected_at),
                    f.finding_type,
                    f.schema_version,
                    f.scope_type,
                    f.scope_key,
                    f.program_key,
                    f.title,
                    f.summary,
                    f.why_now,
                    f.evidence_hash,
                    json.dumps(f.involved_projects, sort_keys=True, ensure_ascii=False),
                    f.suggested_artifact,
                    _serialize_owner_hint(f.owner_hint),
                    closure_kind,
                    closure_json,
                    f.closure_condition_human,
                    f.severity,
                    f.confidence,
                    f.regression_of_finding_id,
                    f.proposal_fingerprint,
                    f.recurrence_count,
                    f.first_seen_cycle_key,
                    f.last_seen_cycle_key,
                    _utc_iso(f.expires_at),
                ),
            )
            await db.execute(
                "INSERT INTO brain_finding_states ("
                " state_id, finding_id, from_state, to_state, actor_user_id, reason"
                ") VALUES (?, ?, NULL, 'open', NULL, NULL)",
                (uuid.uuid4().hex, f.finding_id),
            )
            for pos, ev in enumerate(f.evidence):
                kind, ref = _evidence_kind_for(ev)
                await db.execute(
                    "INSERT OR IGNORE INTO brain_finding_evidence ("
                    " finding_id, position, evidence_kind, evidence_ref,"
                    " weight, cycle_key"
                    ") VALUES (?, ?, ?, ?, 1.0, ?)",
                    (f.finding_id, pos, kind, ref, f.cycle_key),
                )
            persisted += 1
            fingerprints.append(f.proposal_fingerprint)
    return (persisted, fingerprints)


async def _supersede_prior(*, run_id: str, fingerprints: list[str]) -> int:
    """Mark prior `open` findings with the same fingerprint as `superseded`.

    Approved/dismissed/resolved rows are NEVER auto-superseded (human decision
    preserved — sub-04 §8 invariant). Only one new row per fingerprint per
    cycle exists by natural UK; we point predecessors at that new row.
    """
    if not fingerprints:
        return 0
    superseded = 0
    async with write_db() as db:
        for fp in set(fingerprints):
            new_row = await (
                await db.execute(
                    "SELECT finding_id FROM brain_findings"
                    " WHERE run_id = ? AND proposal_fingerprint = ?",
                    (run_id, fp),
                )
            ).fetchone()
            if new_row is None:
                continue
            new_id = new_row[0] if not hasattr(new_row, "keys") else new_row["finding_id"]
            await db.execute(
                "UPDATE brain_findings SET"
                "  approval_state = 'superseded',"
                "  superseded_by_finding_id = ?"
                " WHERE proposal_fingerprint = ?"
                "  AND approval_state = 'open'"
                "  AND finding_id <> ?",
                (new_id, fp, new_id),
            )
            superseded += 1
    return superseded


async def _run_one_rule(
    rule_id: str,
    builder,
    *,
    snapshot: FindingSnapshot,
    run_id: str,
    now: datetime,
    timeout_s: int,
) -> list[FindingDraft]:
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
) -> FindingsReport:
    """Findings phase entry. Caller (jobs._execute_cycle) invokes AFTER the
    Memory-Ops phase has produced operations for this run_id.

    Per-rule isolation: a rule raising or hitting its 15s timeout appends to
    `partial_failures` and lets the cycle continue with the other rules. The
    cycle envelope flips to `partial` if any failure surfaces (jobs.py).
    """
    started = datetime.now(timezone.utc)
    now = (now or started).astimezone(timezone.utc)
    snapshot = await build_snapshot(
        run_id=run_id, cycle_key=cycle_key, workspace_id=workspace_id, as_of=now,
    )

    all_findings: list[Finding] = []
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
                    "kind": "finding_rule_failed",
                    "rule_id": rule_id,
                    "error": "timeout",
                }
            )
            continue
        except Exception as exc:  # noqa: BLE001 — per-rule isolation
            logger.exception("findings: rule %s raised", rule_id)
            partial_failures.append(
                {
                    "kind": "finding_rule_failed",
                    "rule_id": rule_id,
                    "error": str(exc)[:500],
                }
            )
            continue
        for draft in drafts:
            finding = await finalize_finding(
                draft=draft, run_id=run_id, cycle_key=cycle_key, now=now,
            )
            all_findings.append(finding)

    persisted, fingerprints = await _persist_findings(
        run_id=run_id, findings=all_findings
    )
    await _supersede_prior(run_id=run_id, fingerprints=fingerprints)

    return FindingsReport(
        run_id=run_id,
        cycle_key=cycle_key,
        finding_count=persisted,
        partial_failures=partial_failures,
    )


# ---------------------------------------------------------------------------
# Brain v1.2 — Direction integration helpers
# ---------------------------------------------------------------------------


# Confidence numeric -> tier mapping (decisione 2026-05-18, Emilio).
# Threshold to emit finding remains numeric (>= 0.85, high tier).
_CONFIDENCE_TIER_LOW_UPPER = 0.5
_CONFIDENCE_TIER_HIGH_LOWER = 0.85


def map_confidence_to_tier(numeric: float) -> ConfidenceTier:
    """Map a numeric confidence in [0,1] to a categorical tier.

    Mapping (decisione 2026-05-18):
        x < 0.5            -> 'low'
        0.5 <= x < 0.85    -> 'medium'
        x >= 0.85          -> 'high'

    Values outside [0,1] are clamped before mapping. NaN is rejected (raises
    ValueError) to avoid silently surfacing a 'low' tier when the upstream
    LLM returned a bogus value.
    """
    if numeric != numeric:  # NaN guard
        raise ValueError("confidence must not be NaN")
    if numeric < 0.0:
        numeric = 0.0
    elif numeric > 1.0:
        numeric = 1.0
    if numeric < _CONFIDENCE_TIER_LOW_UPPER:
        return "low"
    if numeric < _CONFIDENCE_TIER_HIGH_LOWER:
        return "medium"
    return "high"


async def emit_finding_dedup(
    *,
    finding_type: FindingType,
    entity_ref: str,
    payload: dict[str, Any],
    confidence_numeric: float,
    scope_type: ScopeTypeL4,
    scope_key: str,
    cycle_key: str,
    run_id: str,
    title: str,
    summary: str,
    why_now: str,
    severity: Severity = "medium",
    suggested_artifact: SuggestedArtifact = "none",
    evidence_hash_hex: str | None = None,
    proposal_fingerprint: str | None = None,
    program_key: str | None = None,
    approval_state_new: FindingApprovalState = "open",
    expires_at: datetime | None = None,
    evidence_refs: list[str] | None = None,
) -> tuple[str, bool]:
    """Check-then-boost dedup helper for direction_* findings.

    Semantics (decisione brainstorm §7 / decisione 2026-05-18):
      * Lookup existing finding by (finding_type, entity_ref) where
        approval_state IN ('open', 'pending_bootstrap').
      * If found: UPDATE urgency_score += 1, recurrence_count += 1,
        last_seen_cycle_key = cycle_key, proposed_payload_json = new payload.
      * If not found: INSERT new row with urgency_score=1, recurrence_count=1,
        confidence = map_confidence_to_tier(confidence_numeric).

    Returns (finding_id, was_created).
    """
    if not entity_ref:
        raise ValueError("entity_ref required for dedup emit")

    confidence_tier: ConfidenceTier = map_confidence_to_tier(confidence_numeric)
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)

    async with write_db() as db:
        cur = await db.execute(
            "SELECT finding_id, urgency_score, recurrence_count, approval_state"
            " FROM brain_findings"
            " WHERE finding_type = ? AND entity_ref = ?"
            "   AND approval_state IN ('open', 'pending_bootstrap')"
            " ORDER BY created_at DESC"
            " LIMIT 1",
            (finding_type, entity_ref),
        )
        existing = await cur.fetchone()
        await cur.close()

        if existing is not None:
            finding_id = existing[0]
            await db.execute(
                "UPDATE brain_findings SET"
                "  urgency_score = urgency_score + 1,"
                "  recurrence_count = recurrence_count + 1,"
                "  last_seen_cycle_key = ?,"
                "  proposed_payload_json = ?,"
                "  confidence = ?,"
                "  summary = ?,"
                "  why_now = ?"
                " WHERE finding_id = ?",
                (cycle_key, payload_json, confidence_tier, summary, why_now, finding_id),
            )
            return (finding_id, False)

        # INSERT path
        # Finding.finding_id requires exactly 32 chars (min_length=32,
        # max_length=32), matching the BLAKE2b-16 hex sibling ids
        # (signal_id/operation_id/event_id). Keep the "fnd_" prefix and pad to
        # 32 total chars (4 prefix + 28 hex) so every findings endpoint
        # validates instead of 500-ing.
        finding_id = f"fnd_{uuid.uuid4().hex[:28]}"
        now_iso = _utc_iso(datetime.now(timezone.utc))
        if expires_at is None:
            expires_iso = _utc_iso(
                datetime.now(timezone.utc) + timedelta(days=DEFAULT_OPEN_TTL_DAYS)
            )
        else:
            expires_iso = _utc_iso(expires_at)
        ev_hash = evidence_hash_hex or hashlib.blake2b(
            entity_ref.encode("utf-8"), digest_size=32
        ).hexdigest()
        fp = proposal_fingerprint or hashlib.blake2b(
            f"{finding_type}:{entity_ref}:{cycle_key}".encode("utf-8"),
            digest_size=16,
        ).hexdigest()
        await db.execute(
            "INSERT INTO brain_findings ("
            " finding_id, run_id, cycle_key, detected_at, finding_type,"
            " scope_type, scope_key, program_key, title, summary, why_now,"
            " evidence_hash, suggested_artifact, closure_condition_kind,"
            " closure_condition_json,"
            " severity, confidence, approval_state,"
            " proposal_fingerprint, recurrence_count, first_seen_cycle_key,"
            " last_seen_cycle_key, expires_at,"
            " urgency_score, entity_ref, proposed_payload_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_attest',"
            " ?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?, ?)",
            (
                finding_id,
                run_id,
                cycle_key,
                now_iso,
                finding_type,
                scope_type,
                scope_key,
                program_key,
                title[:200],
                summary[:2000],
                why_now[:500],
                ev_hash,
                suggested_artifact,
                # ClosureManualAttest requires instruction (10..500 chars);
                # a kind persisted without its JSON body is unreadable by
                # _load_closure and surfaces as "legacy closure (parse error)".
                json.dumps(
                    {
                        "kind": "manual_attest",
                        "instruction": (
                            "Verify the underlying state, then resolve with"
                            " attestation or dismiss this finding."
                        ),
                    },
                    sort_keys=True,
                ),
                severity,
                confidence_tier,
                approval_state_new,
                fp,
                cycle_key,
                cycle_key,
                expires_iso,
                entity_ref,
                payload_json,
            ),
        )
        await db.execute(
            "INSERT INTO brain_finding_states ("
            " state_id, finding_id, from_state, to_state, actor_user_id, reason"
            ") VALUES (?, ?, NULL, ?, NULL, NULL)",
            (uuid.uuid4().hex, finding_id, approval_state_new),
        )

        # Finding.evidence requires >=1 item (models.brain.Finding): a
        # finding with 0 brain_finding_evidence rows 500s the strict model
        # on read/patch. Mirror _persist_findings: derive rows from the
        # drift-signal refs, falling back to entity_ref so the invariant
        # always holds.
        refs = evidence_refs or [entity_ref]
        for pos, ev in enumerate(refs):
            kind, ref = _evidence_kind_for(ev)
            await db.execute(
                "INSERT OR IGNORE INTO brain_finding_evidence ("
                " finding_id, position, evidence_kind, evidence_ref,"
                " weight, cycle_key"
                ") VALUES (?, ?, ?, ?, 1.0, ?)",
                (finding_id, pos, kind, ref, cycle_key),
            )

    return (finding_id, True)


__all__ = [
    "DEFAULT_OPEN_TTL_DAYS",
    "DEFAULT_RULE_TIMEOUT_S",
    "PER_TYPE_CAP_DEFAULT",
    "FindingDraft",
    "FindingSnapshot",
    "FindingsReport",
    "REGISTERED_RULES",
    "build_snapshot",
    "emit_finding_dedup",
    "evidence_hash",
    "finalize_finding",
    "make_finding_id",
    "make_proposal_fingerprint",
    "map_confidence_to_tier",
    "run_phase",
]
