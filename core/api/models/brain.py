# Brain v1 — cross-boundary Pydantic v2 contracts (sub-01 Digest + Journal,
# sub-02 Drift, sub-03 Memory Ops, sub-04 Learn Findings).
# Plan: docs/plans/sub/2026-05-15-brain-v1-01-digest-journal.md §4.6
#       docs/plans/sub/2026-05-15-brain-v1-02-drift-checker.md §4.4 / §4.5 / §11.5 (CE4)
#       docs/plans/sub/2026-05-15-brain-v1-03-memory-operations.md §4 / §11 / §11.5 (CE3)
#       docs/plans/sub/2026-05-15-brain-v1-04-learn-findings.md §7 / §11 / §11.5 (CE2)
# Service-private dataclasses live in api/services/brain/models.py.
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

EventType = Literal[
    "file_changed",
    "commit_changed",
    "task_changed",
    "pr_changed",
    "handoff_changed",
    "learning_changed",
    "doc_changed",
    "ingest_changed",
    "kg_changed",
    "regression_signal",
    "external_update_seen",
]

SourceSystem = Literal[
    "ingest",
    "git",
    "kg",
    "pir",
    "handoff",
    "learning",
    "ci",
    "docs_governance",
]

RunStatus = Literal["running", "succeeded", "partial", "failed", "superseded"]
RunTrigger = Literal["batch", "manual", "backfill"]
ScopeType = Literal["company", "program", "project"]


# ---------------------------------------------------------------------------
# Sub-02 Drift Checker (L3)
# ---------------------------------------------------------------------------

# Brain v1.2 (2026-05-18) extends with DR8 (direction_misalignment).
RuleId = Literal["DR1", "DR2", "DR3", "DR4", "DR5", "DR6", "DR7", "DR8", "DR9"]

SignalType = Literal[
    "activity_without_status",
    "decision_without_adr",
    "playbook_changed",
    "stale_open_loop",
    "docs_governance_drift",
    "external_update_unpropagated",
    "claimed_decision_gap",
    "direction_misalignment",
    "task_superseded",
]

KnowledgeForm = Literal[
    "adr",
    "spec",
    "playbook",
    "tribal_memory",
    "external_update",
    "claimed_decision",
    "unknown",
]

Severity = Literal["low", "medium", "high", "critical"]

SignalState = Literal["open", "superseded", "resolved", "dismissed"]

# CE4: extended with `brainstorm` + `meeting_transcript` for v1.2 capture forward-compat.
DirectionSource = Literal[
    "journal",
    "project_status",
    "handoff",
    "doc",
    "brainstorm",
    "meeting_transcript",
    "task",
    "pr",
    "commit",
    "none",
]

# CE4 axis classification (sub-02 §11.5). NULL = pre-CE4 unknown bucket.
DriftAxis = Literal["intent", "context", "both"]


class DigestEvent(BaseModel):
    """Append-only digest event with deterministic BLAKE2b id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=32, max_length=32)
    run_id: str
    cycle_key: str
    observed_at: AwareDatetime
    derived_from_state_at: AwareDatetime
    event_type: EventType
    schema_version: int = 1
    source_system: SourceSystem
    source_project: str | None = None
    target_project: str | None = None
    program_key: str | None = None
    source_ref: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    evidence: dict[str, object] = Field(default_factory=dict)
    evidence_hash: str = Field(min_length=64, max_length=64)


class DigestEventRedacted(BaseModel):
    """Stripped projection of a digest event when caller lacks visibility."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    cycle_key: str
    event_type: EventType
    redacted: Literal[True] = True


class BrainRun(BaseModel):
    """Cycle envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    workspace_id: str
    cycle_key: str
    cycle_window_start_utc: AwareDatetime
    cycle_window_end_utc: AwareDatetime
    cutoff_hour_utc_at_run: int
    scope_type: Literal["company"]
    scope_key: str
    trigger: RunTrigger
    triggered_by: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    status: RunStatus
    superseded_by_run_id: str | None = None
    event_count: int = 0
    partial_failures: list[dict[str, object]] = Field(default_factory=list)
    duration_ms: int | None = None
    error_summary: str | None = None


class JournalBody(BaseModel):
    """Materialized journal body — single SELECT, no read-time composition."""

    model_config = ConfigDict(extra="forbid")

    what_changed: list[dict[str, object]] = Field(default_factory=list)
    decisions_observed: list[str] = Field(default_factory=list)
    open_loops: list[dict[str, object]] = Field(default_factory=list)
    notable_context: list[dict[str, object]] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    tomorrow_watch: list[dict[str, object]] = Field(default_factory=list)


class JournalEntry(BaseModel):
    """Journal entry response envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str
    run_id: str
    workspace_id: str
    cycle_key: str
    scope_type: ScopeType
    scope_key: str
    program_key: str | None = None
    body: JournalBody
    is_empty: bool
    published_at: AwareDatetime
    redacted_count: int = 0
    # Brain v1.1 LLM polish layer (read-time, transient — never persisted).
    # Populated only when BRAIN_LLM_POLISH_ENABLED + sub-toggle journal_polish
    # are on AND the idempotency cache has a successful entry for this run.
    narrative_polished: str | None = None
    cited_evidence_refs: list[str] | None = None
    polish_model: str | None = None
    # Brain agent-native (decision 2026-07-01): narrative written by the user's
    # own agent; provenance kept SEPARATE from the cycle's narrative_polished
    # (migration 158) so the two never overwrite each other. body (deterministic)
    # is always present, so the surfaced narrative is never null.
    narrative_agent: str | None = None
    narrative_agent_at: str | None = None
    narrative_agent_by: str | None = None


class EventsListResponse(BaseModel):
    """D6 GET /api/v1/brain/events response envelope."""

    model_config = ConfigDict(extra="forbid")

    items: list[DigestEvent | DigestEventRedacted] = Field(default_factory=list)
    next_cursor: str | None = None
    cycle_key: str | None = None
    run_id: str | None = None
    redacted_count: int = 0
    total_returned: int = 0


class JournalListResponse(BaseModel):
    """GET /api/v1/brain/journal response envelope (sub-05 §2)."""

    model_config = ConfigDict(extra="forbid")

    items: list[JournalEntry] = Field(default_factory=list)
    next_cursor: str | None = None
    cycle_key: str | None = None
    run_id: str | None = None
    total_returned: int = 0


# ---------------------------------------------------------------------------
# Drift Signals (sub-02)
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    """Typed evidence reference cited by a drift signal.

    `kind` is open-ended in v1 — common values: 'event' (digest event_id),
    'handoff', 'doc', 'audit_log'. `ref` is the canonical TEXT identifier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    ref: str = Field(min_length=1, max_length=512)
    note: str | None = Field(default=None, max_length=512)


class BaselineReference(BaseModel):
    """Output of C2 baseline resolver.

    Always returns a value — `source='none'` sentinel replaces nullable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: DirectionSource = "none"
    ref: str | None = None
    state_at: AwareDatetime | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    conflict: bool = False
    secondary_refs: list[str] = Field(default_factory=list)


class DriftSignal(BaseModel):
    """Drift signal contract (sub-02 §4.4) with CE4 axis (§11.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str = Field(min_length=32, max_length=32)
    run_id: str
    cycle_key: str
    detected_at: AwareDatetime
    rule_id: RuleId
    schema_version: int = 1
    scope_type: ScopeType
    scope_key: str
    program_key: str | None = None
    signal_type: SignalType
    knowledge_form: KnowledgeForm
    classifier_version: int = 1
    expected_direction_source: DirectionSource = "none"
    expected_direction_ref: str | None = None
    observed_direction_ref: str = Field(min_length=1)
    observed_delta: str = Field(max_length=2000)
    evidence: list[str] = Field(default_factory=list)
    evidence_hash: str = Field(min_length=64, max_length=64)
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    recurrence_key: str = Field(min_length=16, max_length=16)
    involved_projects: list[str] = Field(default_factory=list)
    state: SignalState = "open"
    superseded_by_signal_id: str | None = None
    resolved_at: AwareDatetime | None = None
    dismissed_at: AwareDatetime | None = None
    dismissed_by: str | None = None
    dismiss_reason: str | None = None
    # CE4 axis (§11.5): nullable for legacy rows pre-CE4.
    drift_axis: DriftAxis | None = None


class DriftSignalRedacted(BaseModel):
    """Stripped projection when caller lacks visibility on all
    `involved_projects`. Mirror sub-01 DigestEventRedacted shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str
    cycle_key: str
    signal_type: SignalType
    severity: Severity
    redacted: Literal[True] = True


class DriftListResponse(BaseModel):
    """GET /api/v1/brain/drift response envelope."""

    model_config = ConfigDict(extra="forbid")

    items: list[DriftSignal | DriftSignalRedacted] = Field(default_factory=list)
    next_cursor: str | None = None
    cycle_key: str | None = None
    run_id: str | None = None
    redacted_count: int = 0
    total_returned: int = 0


DriftPatchAction = Literal["dismiss", "acknowledge", "resolve", "reopen"]


class DriftPatchRequest(BaseModel):
    """Lifecycle PATCH body (sub-02 C5)."""

    model_config = ConfigDict(extra="forbid")

    action: DriftPatchAction
    reason: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Sub-03 Memory Operations (L4)
# ---------------------------------------------------------------------------

# 8 active enum values: M1-M7 + CE3 cascade_rollup. compression_candidate is
# the §11.5 CE3 sibling (active per migration 129 CHECK), kept here for forward
# compat — its rule producer is deferred to v1.04.
# 2 reserved literals (deduplicate, promotion_candidate) live ONLY in the
# Literal type for forward-compat — NOT accepted by the SQL CHECK.
OperationType = Literal[
    "reinforce",
    "consolidate",
    "supersede_candidate",
    "provenance_hardening",
    "orphan_detected",
    "contradiction_detected",
    "cascade_rollup",
    "compression_candidate",
    # v1.04 reserved (no producer, no CHECK acceptance):
    "deduplicate",
    "promotion_candidate",
]

ApprovalState = Literal[
    "pending",
    "approved",
    "rejected",
    "dismissed",
    "superseded",
    "expired",
    "applied",
    "reverted",
]

ScopeTypeL4 = Literal["company", "program", "project", "artifact"]

ProposedWriteTarget = Literal[
    "none",
    "task",
    "guide",
    "adr",
    "learning",
    "kg_edge_metric",
    "doc_patch",
    "context_md_append",
]

# Direction matrix derived from operation_type (§4.X — NOT a persisted column).
MyelinDirection = Literal[
    "strengthen", "connect", "split", "promote",
    "quarantine_candidate", "canonicalize",
]


class ProposedWriteNone(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["none"] = "none"


class ProposedWriteTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["task"] = "task"
    title: str = Field(min_length=1, max_length=200)
    description: str
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    project: str
    delegation: Literal["agent", "hybrid", "human"] = "hybrid"
    impact: int = Field(ge=1, le=10)
    confidence: int = Field(ge=1, le=10)
    ease: int = Field(ge=1, le=10)
    tags: list[str] = Field(default_factory=list)


class ProposedWriteGuide(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["guide"] = "guide"
    path: str
    title: str
    body: str = Field(min_length=100)
    source_refs: list[str] = Field(min_length=1)


class ProposedWriteADR(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["adr"] = "adr"
    path: str
    title: str
    decision: str
    context: str
    consequences: str
    supersedes: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(min_length=1)


class ProposedWriteLearning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["learning"] = "learning"
    title: str
    category: Literal[
        "deploy", "migration", "auth", "testing",
        "architecture", "security", "performance", "process",
    ]
    description: str
    prevention: str = Field(min_length=10)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    module: str | None = None
    tags: list[str] = Field(default_factory=list)
    project: str | None = None


class ProposedWriteKGEdgeMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["kg_edge_metric"] = "kg_edge_metric"
    edge_id: str
    metric_kind: Literal["touch_count", "reinforce_score", "decay_score"]
    delta: float = Field(ge=-1.0, le=1.0)


class ProposedWriteDocPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["doc_patch"] = "doc_patch"
    path: str
    unified_diff: str = Field(min_length=10)
    base_sha: str
    rationale: str


class ProposedWriteContextMdAppend(BaseModel):
    """CE3 M8 cascade rollup target — append-only patch to parent context.md."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    target_type: Literal["context_md_append"] = "context_md_append"
    path: str
    body: str = Field(min_length=1)
    rollup_cycle_key: str
    child_entry_ids: list[str] = Field(min_length=1)


ProposedWrite = Annotated[
    ProposedWriteNone
    | ProposedWriteTask
    | ProposedWriteGuide
    | ProposedWriteADR
    | ProposedWriteLearning
    | ProposedWriteKGEdgeMetric
    | ProposedWriteDocPatch
    | ProposedWriteContextMdAppend,
    Field(discriminator="target_type"),
]


class MyelinEffect(BaseModel):
    """Read-time projection: derived `direction` + persisted `score`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: MyelinDirection
    score: float = Field(ge=0.0, le=1.0)


class MemoryOperation(BaseModel):
    """Memory operation contract (sub-03 §4.4) with CE3 cascade_rollup (§11.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=32, max_length=32)
    run_id: str
    cycle_key: str
    detected_at: AwareDatetime
    operation_type: OperationType
    schema_version: int = 1
    scope_type: ScopeTypeL4
    scope_key: str
    program_key: str | None = None
    source_ref: str = Field(min_length=1)
    target_ref: str = ""
    score: float = Field(ge=0.0, le=1.0)
    recurrence_key: str = Field(min_length=16, max_length=16)
    recurrence_count: int = Field(default=1, ge=1)
    first_seen_cycle_key: str
    last_seen_cycle_key: str
    involved_projects: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(min_length=1)
    evidence_hash: str = Field(min_length=64, max_length=64)
    summary: str = Field(max_length=2000)
    proposed_write: ProposedWrite
    myelin_effect: MyelinEffect
    requires_approval: bool = True
    approval_state: ApprovalState = "pending"
    expires_at: AwareDatetime
    superseded_by_operation_id: str | None = None
    applied_at: AwareDatetime | None = None
    applied_by_user_id: str | None = None
    applied_artifact_ref: str | None = None


class MemoryOperationRedacted(BaseModel):
    """Stripped projection when caller lacks visibility on all
    `involved_projects` (sub-03 §4.3 ALL-or-redacted)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str
    cycle_key: str
    operation_type: OperationType
    redacted: Literal[True] = True


class MemoryOperationsListResponse(BaseModel):
    """GET /api/v1/brain/memory-operations response envelope."""

    model_config = ConfigDict(extra="forbid")

    items: list[MemoryOperation | MemoryOperationRedacted] = Field(default_factory=list)
    next_cursor: str | None = None
    cycle_key: str | None = None
    run_id: str | None = None
    redacted_count: int = 0
    total_returned: int = 0


MemoryOpPatchAction = Literal["approve", "dismiss", "reject"]


class MemoryOpPatchRequest(BaseModel):
    """PATCH /api/v1/brain/memory-operations/{operation_id} body (sub-03 §11.1)."""

    model_config = ConfigDict(extra="forbid")

    approval_state: MemoryOpPatchAction
    reason: str | None = Field(default=None, max_length=500)
    applied_artifact_ref: str | None = Field(default=None, max_length=500)


class ApplyNextAction(BaseModel):
    """Guidance returned by POST /apply — NOT a write."""

    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    args: dict[str, object] = Field(default_factory=dict)
    rationale: str = ""
    must_include_in_tags: str
    target_path: str | None = None
    body: str | None = None


class ApplyResponse(BaseModel):
    """POST /api/v1/brain/memory-operations/{operation_id}/apply response.

    Also reused by sub-04 Findings apply endpoint — the `operation_id` slot
    carries either the operation_id (sub-03) or the finding_id (sub-04).
    The router is responsible for populating the correct identifier; the
    payload shape is intentionally identical so MCP clients consume one
    envelope across both surfaces.
    """

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    next_action: ApplyNextAction
    operation_summary: dict[str, object] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sub-04 Learn Findings (L5)
# ---------------------------------------------------------------------------

# Finding type enum values (sub-04 §2 / §10.X CHECK).
# Brain v1.2 (2026-05-18) extends with direction_drift + direction_bootstrap.
FindingType = Literal[
    "idea",
    "task_candidate",
    "open_question",
    "scope_gap",
    "procedure_change",
    "contradiction",
    "direction_drift",
    "direction_bootstrap",
    "task_probably_done",
]

# Lifecycle states. system-driven: superseded, expired. operator-driven:
# approved, dismissed, resolved. Initial: open.
# Brain v1.2 (2026-05-18) extends with pending_bootstrap + applied
# (bootstrap rollout + direction_drift write-back states).
FindingApprovalState = Literal[
    "open",
    "approved",
    "dismissed",
    "resolved",
    "superseded",
    "expired",
    "pending_bootstrap",
    "applied",
]

# Operator-driven PATCH actions (system supersede/expire happens off-path).
FindingPatchAction = Literal["approved", "dismissed", "resolved"]

SuggestedArtifact = Literal[
    "task",
    "adr",
    "guide",
    "learning",
    "status_update",
    "question",
    "none",
]

# F10 / FR1 anti-anti-pattern: confidence is a TIER (categorical), NEVER
# float. The CHECK constraint in migration 130 enforces the same at storage
# level. UI shows a tier badge; MCP returns the tier verbatim.
ConfidenceTier = Literal["low", "medium", "high"]

# Sub-04 §7.5 owner_hint provenance — non-binding, deterministic.
OwnerHintSource = Literal["kg_hotspot", "project_default", "none"]

# Sub-04 §7.1 closure_condition discriminated union.
ClosureConditionKind = Literal[
    "drift_signal_clears",
    "memory_op_applied",
    "artifact_exists",
    "manual_attest",
]


class ClosureDriftSignalClears(BaseModel):
    """Finding closes when the cited drift signal stops firing (sub-04 §7.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["drift_signal_clears"] = "drift_signal_clears"
    drift_signal_id: str = Field(min_length=1)
    consecutive_clear_cycles: int = Field(default=1, ge=1, le=14)


class ClosureMemoryOpApplied(BaseModel):
    """Finding closes when the cited memory op transitions to applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["memory_op_applied"] = "memory_op_applied"
    memory_operation_id: str = Field(min_length=1)


class ArtifactSelector(BaseModel):
    """One-of selector for ClosureArtifactExists (sub-04 §7.1).

    All fields nullable; the auto-resolve detector picks the first non-null
    selector during the closure observable check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    slug: str | None = None
    path_glob: str | None = None
    title_match: str | None = None
    tag_match: str | None = None  # e.g. "brain_finding:{finding_id}"


class ClosureArtifactExists(BaseModel):
    """Finding closes when a derived artifact tagged with this finding_id appears."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["artifact_exists"] = "artifact_exists"
    artifact_kind: Literal[
        "task", "learning", "adr", "guide", "status_update", "handoff"
    ]
    selector: ArtifactSelector


class ClosureManualAttest(BaseModel):
    """Operator clicks Resolve with attestation text (sub-04 §7.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["manual_attest"] = "manual_attest"
    instruction: str = Field(min_length=10, max_length=500)


ClosureCondition = Annotated[
    ClosureDriftSignalClears
    | ClosureMemoryOpApplied
    | ClosureArtifactExists
    | ClosureManualAttest,
    Field(discriminator="kind"),
]


class OwnerHint(BaseModel):
    """KG hotspot → deterministic owner hint (sub-04 §7.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str | None = None
    team: str | None = None
    project: str | None = None
    source: OwnerHintSource = "none"
    touch_count: int | None = Field(default=None, ge=0)
    alternates: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """Learn finding contract (sub-04 §7).

    `confidence` and `severity` are categorical TIERS. NEVER multiply them
    into a composite score (sub-04 §7.4 / §11.5 anti-pattern: range
    compression).
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=32, max_length=32)
    run_id: str
    cycle_key: str
    detected_at: AwareDatetime
    finding_type: FindingType
    schema_version: int = 1
    scope_type: ScopeTypeL4
    scope_key: str
    program_key: str | None = None
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=2000)
    why_now: str = Field(max_length=500)
    evidence: list[str] = Field(min_length=1, max_length=50)
    evidence_hash: str = Field(min_length=64, max_length=64)
    involved_projects: list[str] = Field(default_factory=list)
    suggested_artifact: SuggestedArtifact
    owner_hint: OwnerHint | None = None
    closure_condition: ClosureCondition
    closure_condition_human: str | None = Field(default=None, max_length=500)
    severity: Severity
    # FR1/F10 binding contract: tier, not float.
    confidence: ConfidenceTier
    approval_state: FindingApprovalState = "open"
    regression_of_finding_id: str | None = None
    proposal_fingerprint: str = Field(min_length=32, max_length=32)
    recurrence_count: int = Field(default=1, ge=1)
    first_seen_cycle_key: str
    last_seen_cycle_key: str
    applied_artifact_ref: str | None = None
    applied_at: AwareDatetime | None = None
    applied_by_user_id: str | None = None
    expires_at: AwareDatetime
    superseded_by_finding_id: str | None = None
    # Brain agent-native (decision 2026-07-01): set when the finding was written
    # by the user's own agent via brain_write_finding; None for findings produced
    # by the mechanical cycle rules (migration 159). Separate provenance so agent
    # conclusions are distinguishable and queryable.
    authored_by_agent: str | None = None
    # CE2 §11.5 — read-time only, never persisted. Populated by
    # findings_reader.list_findings() when the decay setting is enabled.
    recency_factor: float | None = Field(default=None, ge=0.0, le=1.0)
    # Brain v1.1 LLM polish layer (read-time, transient — never persisted).
    summary_polished: str | None = None
    why_now_polished: str | None = None
    reasoning_polished: str | None = None
    cited_evidence_refs: list[str] | None = None
    polish_model: str | None = None


class FindingRedacted(BaseModel):
    """Stripped projection when caller lacks visibility on all
    `involved_projects` (sub-04 §7.3 ALL-or-redacted)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str
    cycle_key: str
    finding_type: FindingType
    severity: Severity
    redacted: Literal[True] = True


class FindingsListResponse(BaseModel):
    """GET /api/v1/brain/findings response envelope (sub-04 §11.1)."""

    model_config = ConfigDict(extra="forbid")

    items: list[Finding | FindingRedacted] = Field(default_factory=list)
    next_cursor: str | None = None
    cycle_key: str | None = None
    run_id: str | None = None
    redacted_count: int = 0
    redacted_evidence_count: int = 0
    # Data-integrity guard: rows that fail the Finding contract at read time
    # (e.g. a malformed finding_id — the DB PK has no length CHECK) are skipped
    # instead of 500-ing the page, and counted here so a poison row stays
    # visible to the caller and drains never silently stall (audit 2026-08-14).
    malformed_count: int = 0
    total_returned: int = 0


class FindingPatchRequest(BaseModel):
    """PATCH /api/v1/brain/findings/{finding_id} body (sub-04 §11.1)."""

    model_config = ConfigDict(extra="forbid")

    approval_state: FindingPatchAction
    reason: str | None = Field(default=None, max_length=500)
    applied_artifact_ref: str | None = Field(default=None, max_length=500)


class FindingBulkPatchRequest(BaseModel):
    """PATCH /api/v1/brain/findings:bulk body (sub-04 §11.1)."""

    model_config = ConfigDict(extra="forbid")

    finding_ids: list[str] = Field(min_length=1, max_length=25)
    approval_state: FindingPatchAction
    reason: str | None = Field(default=None, max_length=500)


class FindingBulkResultEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    finding_id: str
    status: FindingApprovalState | Literal["skipped"]


class FindingBulkPatchResponse(BaseModel):
    """Bulk PATCH response — per-finding result."""

    model_config = ConfigDict(extra="forbid")

    results: list[FindingBulkResultEntry] = Field(default_factory=list)
    applied_count: int = 0
    skipped_count: int = 0


# ---------------------------------------------------------------------------
# Sub-05 Surfaces (runs / counters / recompute / capabilities)
# ---------------------------------------------------------------------------


class RunsListResponse(BaseModel):
    """GET /api/v1/brain/runs response envelope (sub-05 §2)."""

    model_config = ConfigDict(extra="forbid")

    items: list[BrainRun] = Field(default_factory=list)
    next_cursor: str | None = None
    cycle_key: str | None = None
    total_returned: int = 0


class PipelineCounters(BaseModel):
    """GET /api/v1/brain/counters response — aggregated 6-station counts.

    Single-call replacement for 5 concurrent fetches (sub-05 v1.1 optimization).
    PipelineSubbar uses this to populate every station counter in one round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    cycle_key: str
    run_id: str | None = None
    ingest: int = 0
    digest: int = 0
    journal: int = 0
    drift: int = 0
    memory_ops: int = 0
    findings: int = 0


class RecomputeRequest(BaseModel):
    """POST /api/v1/brain/cycles/{cycle_key}/recompute body (sub-01 D4)."""

    model_config = ConfigDict(extra="forbid")

    sources: list[Literal["digest", "drift", "memory_ops", "learn"]] | None = None
    force: bool = False
    dry_run: bool = False


class RecomputeResponse(BaseModel):
    """POST recompute response envelope (sub-01 D4)."""

    model_config = ConfigDict(extra="forbid")

    status: str
    cycle_key: str
    run_id: str | None = None
    event_count: int = 0
    journal_count: int = 0
    duration_ms: int | None = None
    error_kind: str | None = None
    mode: str | None = None
    dry_run: bool = False


class BrainCapabilities(BaseModel):
    """GET /api/v1/brain/capabilities response — schema metadata for agents.

    Mirrors graph_capabilities precedent — agents can discover enums + node
    kinds at cold start without hardcoding constants. Returned values are the
    canonical Literal members enforced at the Pydantic boundary.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_types: list[str] = Field(default_factory=list)
    source_systems: list[str] = Field(default_factory=list)
    signal_types: list[str] = Field(default_factory=list)
    knowledge_forms: list[str] = Field(default_factory=list)
    operation_types: list[str] = Field(default_factory=list)
    finding_types: list[str] = Field(default_factory=list)
    severities: list[str] = Field(default_factory=list)
    confidence_tiers: list[str] = Field(default_factory=list)
    drift_axes: list[str] = Field(default_factory=list)
    approval_states: list[str] = Field(default_factory=list)
    finding_approval_states: list[str] = Field(default_factory=list)
    signal_states: list[str] = Field(default_factory=list)
    run_statuses: list[str] = Field(default_factory=list)
    run_triggers: list[str] = Field(default_factory=list)
    scope_types: list[str] = Field(default_factory=list)
    suggested_artifacts: list[str] = Field(default_factory=list)
    closure_condition_kinds: list[str] = Field(default_factory=list)
    knowledge_glyphs: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "ApplyNextAction",
    "ApplyResponse",
    "ApprovalState",
    "ArtifactSelector",
    "BaselineReference",
    "BrainCapabilities",
    "BrainRun",
    "ClosureArtifactExists",
    "ClosureCondition",
    "ClosureConditionKind",
    "ClosureDriftSignalClears",
    "ClosureManualAttest",
    "ClosureMemoryOpApplied",
    "ConfidenceTier",
    "DigestEvent",
    "DigestEventRedacted",
    "DirectionSource",
    "DriftAxis",
    "DriftListResponse",
    "DriftPatchAction",
    "DriftPatchRequest",
    "DriftSignal",
    "DriftSignalRedacted",
    "EventType",
    "EventsListResponse",
    "EvidenceItem",
    "Finding",
    "FindingApprovalState",
    "FindingBulkPatchRequest",
    "FindingBulkPatchResponse",
    "FindingBulkResultEntry",
    "FindingPatchAction",
    "FindingPatchRequest",
    "FindingRedacted",
    "FindingType",
    "FindingsListResponse",
    "JournalBody",
    "JournalEntry",
    "JournalListResponse",
    "KnowledgeForm",
    "MemoryOpPatchAction",
    "MemoryOpPatchRequest",
    "MemoryOperation",
    "MemoryOperationRedacted",
    "MemoryOperationsListResponse",
    "MyelinDirection",
    "MyelinEffect",
    "OperationType",
    "OwnerHint",
    "OwnerHintSource",
    "PipelineCounters",
    "ProposedWrite",
    "ProposedWriteADR",
    "ProposedWriteContextMdAppend",
    "ProposedWriteDocPatch",
    "ProposedWriteGuide",
    "ProposedWriteKGEdgeMetric",
    "ProposedWriteLearning",
    "ProposedWriteNone",
    "ProposedWriteTarget",
    "ProposedWriteTask",
    "RecomputeRequest",
    "RecomputeResponse",
    "RuleId",
    "RunStatus",
    "RunTrigger",
    "RunsListResponse",
    "ScopeType",
    "ScopeTypeL4",
    "Severity",
    "SignalState",
    "SignalType",
    "SourceSystem",
    "SuggestedArtifact",
]
