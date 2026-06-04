// MarvisX Console — Brain v1 TypeScript types
// Pre-implementation bozza. Da copiare in console/src/lib/brain/types.ts
// quando si apre il worktree per task UI.
//
// Coerente con sub-01-04 §4 Pydantic v2 contracts (parent plan §5).
// BLAKE2b stable hashes per tutti i *_id cross-layer references.
//
// Source of truth: /data/projects/marvisx/docs/plans/sub/2026-05-15-brain-v1-*.md

// ============================================================================
// Common enums (Literal types — mirror Python `api/models/brain.py`)
// ============================================================================

export type ScopeType = "company" | "program" | "project";
export type ScopeTypeL4 = "company" | "program" | "project" | "artifact"; // L4 adds artifact

export type RunStatus =
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "superseded";

export type TriggerKind = "batch" | "manual" | "backfill";

// L2 Digest
export type EventType =
  | "file_changed"
  | "commit_changed"
  | "task_changed"
  | "pr_changed"
  | "handoff_changed"
  | "learning_changed"
  | "doc_changed"
  | "ingest_changed"
  | "kg_changed"
  | "regression_signal" // v1.1 deferred producer
  | "external_update_seen"; // v1.1 deferred producer

export type SourceSystem =
  | "ingest"
  | "git"
  | "kg"
  | "pir"
  | "handoff"
  | "learning"
  | "ci"
  | "docs_governance";

// L3 Drift
export type RuleId = "DR1" | "DR2" | "DR3" | "DR4" | "DR5" | "DR6" | "DR7";

export type SignalType =
  | "activity_without_status"
  | "decision_without_adr"
  | "playbook_changed"
  | "stale_open_loop"
  | "docs_governance_drift"
  | "external_update_unpropagated"
  | "claimed_decision_gap";

export type KnowledgeForm =
  | "adr"
  | "spec"
  | "playbook"
  | "tribal_memory"
  | "external_update"
  | "claimed_decision"
  | "unknown";

export type Severity = "low" | "medium" | "high" | "critical";

export type SignalState = "open" | "superseded" | "resolved" | "dismissed";

export type DirectionSource =
  | "journal"
  | "project_status"
  | "handoff"
  | "doc"
  | "task"
  | "pr"
  | "commit"
  | "none";

// L4 Memory Operations
export type OperationType =
  // 6 active v1
  | "reinforce"
  | "consolidate"
  | "supersede_candidate"
  | "provenance_hardening"
  | "orphan_detected"
  | "contradiction_detected"
  // 3 v1.04 reserved (Literal forward-compat, no producer in v1.03)
  | "deduplicate"
  | "promotion_candidate"
  | "compression_candidate";

export type ApprovalStateMemoryOp =
  | "pending"
  | "approved"
  | "rejected"
  | "dismissed"
  | "superseded"
  | "expired"
  | "applied" // L4 has applied because ops propose writes
  | "reverted";

export type ProposedWriteTarget =
  | "none"
  | "task"
  | "guide"
  | "adr"
  | "learning"
  | "kg_edge_metric"
  | "doc_patch";

export type MyelinDirection =
  | "strengthen"
  | "weaken"
  | "connect"
  | "split"
  | "promote"
  | "quarantine_candidate";

// L5 Findings
export type FindingType =
  | "idea"
  | "task_candidate"
  | "open_question"
  | "scope_gap" // L5 only — removed from L4
  | "procedure_change"
  | "contradiction";

export type ApprovalStateFinding =
  | "open" // L5 uses "open" instead of "pending"
  | "approved"
  | "dismissed"
  | "resolved" // L5 has resolved because conditions clear
  | "superseded"
  | "expired";

export type ConfidenceTier = "low" | "medium" | "high"; // L5 uses tier, not float

export type SuggestedArtifact =
  | "task"
  | "adr"
  | "guide"
  | "learning"
  | "status_update"
  | "question"
  | "none";

export type ClosureKind =
  | "drift_signal_clears"
  | "memory_op_applied"
  | "artifact_exists"
  | "manual_attest";

// ============================================================================
// Discriminated unions
// ============================================================================

// L5 Closure condition — discriminated by `kind`
export type ClosureCondition =
  | { kind: "drift_signal_clears"; signal_id: string }
  | { kind: "memory_op_applied"; operation_id: string }
  | { kind: "artifact_exists"; artifact_ref: string }
  | { kind: "manual_attest"; reason?: string };

// L4 ProposedWrite — discriminated by `target_type` (sub-03 §4.5)
export type ProposedWrite =
  | { target_type: "none" }
  | {
      target_type: "task";
      title: string;
      description: string;
      priority: "low" | "medium" | "high" | "critical";
      project: string;
      delegation: "agent" | "hybrid" | "human";
      impact: number; // 1..10
      confidence: number; // 1..10
      ease: number; // 1..10
      tags: string[]; // MUST include `brain_op:{operation_id}`
    }
  | {
      target_type: "guide";
      path: string;
      title: string;
      body: string;
      source_refs: string[];
    }
  | {
      target_type: "adr";
      path: string;
      title: string;
      decision: string;
      context: string;
      consequences: string;
      supersedes: string[];
      source_refs: string[];
    }
  | {
      target_type: "learning";
      title: string;
      category:
        | "deploy"
        | "migration"
        | "auth"
        | "testing"
        | "architecture"
        | "security"
        | "performance"
        | "process";
      description: string;
      prevention: string;
      severity: Severity;
      module?: string;
      tags: string[]; // MUST include `brain_op:{operation_id}`
      project?: string;
    }
  | {
      target_type: "kg_edge_metric";
      edge_id: string;
      metric_kind: "touch_count" | "reinforce_score" | "decay_score";
      delta: number; // -1..1
    }
  | {
      target_type: "doc_patch";
      path: string;
      unified_diff: string;
      base_sha: string;
      rationale: string;
    };

// Evidence ref typed kind (sub-04 §10.X evidence join table enum)
export type EvidenceKind =
  | "digest_event"
  | "journal_entry"
  | "drift_signal"
  | "memory_operation"
  | "kg_node"
  | "handoff"
  | "learning"
  | "audit_log"
  | "task"
  | "pr"
  | "commit"
  | "doc";

export interface EvidenceRef {
  kind: EvidenceKind;
  ref: string; // BLAKE2b hex or canonical TEXT ref
  weight?: number; // 0..1
  cycle_key?: string;
  position?: number;
}

// ============================================================================
// L0 — Run envelope (sub-01 §5.5)
// ============================================================================

export interface BrainRun {
  run_id: string;
  workspace_id: string;
  cycle_key: string; // YYYY-MM-DD
  cycle_window_start_utc: string; // ISO
  cycle_window_end_utc: string;
  cutoff_hour_utc_at_run: number;
  scope_type: "company";
  scope_key: "__company__";
  trigger: TriggerKind;
  triggered_by?: string;
  started_at: string;
  finished_at?: string;
  status: RunStatus;
  superseded_by_run_id?: string;
  event_count: number;
  partial_failures: Array<{ kind: string; error: string }>;
  duration_ms?: number;
  error_summary?: string;
}

// ============================================================================
// L2 — Digest events + Journal entries (sub-01 §4, §5)
// ============================================================================

export interface DigestEvent {
  event_id: string; // BLAKE2b 16-byte hex
  run_id: string;
  cycle_key: string;
  observed_at: string; // ISO with Z suffix
  derived_from_state_at: string;
  event_type: EventType;
  schema_version: number;
  source_system: SourceSystem;
  source_project?: string;
  target_project?: string;
  program_key?: string;
  source_ref: string; // canonical per event_type (sub-01 §3 mapping)
  title: string;
  summary: string;
  evidence: Record<string, unknown>;
  evidence_hash: string; // sha256 64-char
}

export interface JournalEntryBody {
  what_changed: EvidenceRef[];
  decisions_observed: EvidenceRef[]; // whitelist per sub-01 §5.1
  open_loops: EvidenceRef[];
  notable_context: EvidenceRef[];
  sources: string[]; // event_id refs
  tomorrow_watch: EvidenceRef[]; // recomputed per cycle, not persisted state
}

export interface JournalEntry {
  entry_id: string;
  run_id: string;
  workspace_id: string;
  cycle_key: string;
  scope_type: ScopeType;
  scope_key: string; // sentinel `__company__` for company
  program_key?: string;
  body_json: JournalEntryBody;
  is_empty: boolean;
  published_at: string;
  // Brain v1.1 LLM polish layer (transient, read-time only — never persisted)
  narrative_polished?: string;
  cited_evidence_refs?: string[];
  polish_model?: string;
}

// ============================================================================
// L3 — Drift signals (sub-02 §4)
// ============================================================================

export interface DriftSignal {
  signal_id: string; // BLAKE2b hex
  run_id: string;
  cycle_key: string;
  detected_at: string;
  rule_id: RuleId;
  schema_version: number;
  scope_type: ScopeType;
  scope_key: string;
  program_key?: string;
  signal_type: SignalType;
  knowledge_form: KnowledgeForm;
  classifier_version: number;
  expected_direction_source: DirectionSource;
  expected_direction_ref?: string;
  observed_direction_ref: string;
  observed_delta: string;
  evidence: string[];
  evidence_hash: string;
  severity: Severity;
  confidence: number; // 0..1 — L3 uses float (deterministic)
  recurrence_key: string;
  involved_projects: string[];
  state: SignalState;
  superseded_by_signal_id?: string;
  resolved_at?: string;
}

// ============================================================================
// L4 — Memory operations (sub-03 §4)
// ============================================================================

export interface MemoryOperation {
  operation_id: string; // BLAKE2b hex
  run_id: string;
  cycle_key: string;
  detected_at: string;
  operation_type: OperationType;
  schema_version: number;
  scope_type: ScopeTypeL4;
  scope_key: string;
  program_key?: string;
  source_ref: string;
  target_ref: string; // sentinel "" for no-target
  score: number; // 0..1 deterministic
  recurrence_key: string;
  involved_projects: string[];
  evidence: string[]; // min 1
  evidence_hash: string;
  summary: string;
  proposed_write: ProposedWrite;
  // requires_approval is always 1 in v1 (CHECK enforced) — not exposed
  approval_state: ApprovalStateMemoryOp;
  expires_at: string;
  superseded_by_operation_id?: string;

  // Derived (read-time)
  myelin_direction?: MyelinDirection; // derived from operation_type via matrix
  recurrence_count?: number; // computed via recurrence_key grouping
}

// ============================================================================
// L5 — Brain findings (sub-04 §7)
// ============================================================================

export interface BrainFinding {
  finding_id: string; // BLAKE2b hex
  run_id: string;
  cycle_key: string;
  detected_at: string;
  finding_type: FindingType;
  schema_version: number;
  scope_type: ScopeTypeL4;
  scope_key: string;
  program_key?: string;
  title: string;
  why_now: string;
  evidence: string[]; // min 1; cross-layer refs (event/signal/operation)
  evidence_hash: string;
  suggested_artifact: SuggestedArtifact;
  closure_condition: ClosureCondition;
  closure_condition_human?: string;
  confidence: ConfidenceTier; // L5 uses tier
  recurrence_key: string;
  recurrence_count: number;
  involved_projects: string[];
  approval_state: ApprovalStateFinding;
  expires_at: string;
  superseded_by_finding_id?: string;
  regression_of_finding_id?: string; // Sentry-style
  owner_hint?: string;
  // Brain v1.1 LLM polish layer (transient, read-time only — never persisted)
  summary_polished?: string;
  why_now_polished?: string;
  reasoning_polished?: string;
  cited_evidence_refs?: string[];
  polish_model?: string;
}

// ============================================================================
// Apply guidance response (sub-03/04 §11 apply endpoints — GUIDANCE-ONLY)
// ============================================================================

export interface ApplyGuidance {
  operation_id?: string; // when applying memory_op
  finding_id?: string; // when applying finding
  approval_state: "approved";
  next_action: {
    tool: string | null; // e.g., "mcp__marvis__create_task" | null (manual)
    args: Record<string, unknown> | null;
    must_include_in_tags: string; // `brain_op:{id}` | `brain_finding:{id}`
    rationale: string;
  };
  operation_summary?: {
    operation_type: OperationType;
    proposed_write_summary: string;
  };
  finding_summary?: {
    finding_type: FindingType;
    title: string;
  };
}

// ============================================================================
// Response envelopes (cross-cutting per sub-05 §5.1)
// ============================================================================

export interface BrainListResponse<T> {
  cycle_key: string;
  run_id: string;
  next_cursor: string | null;
  redacted_count: number;
  redacted_evidence_count?: number;
  total_estimate: number;
  // Per-resource list field name varies (events / entries / signals / operations / findings / runs):
  events?: DigestEvent[];
  entries?: JournalEntry[];
  signals?: DriftSignal[];
  operations?: MemoryOperation[];
  findings?: BrainFinding[];
  runs?: BrainRun[];
}

// Generic typed alias
export type DigestListResponse = BrainListResponse<DigestEvent> & { events: DigestEvent[] };
export type JournalListResponse = BrainListResponse<JournalEntry> & { entries: JournalEntry[] };
export type DriftListResponse = BrainListResponse<DriftSignal> & { signals: DriftSignal[] };
export type MemoryOpsListResponse = BrainListResponse<MemoryOperation> & { operations: MemoryOperation[] };
export type FindingsListResponse = BrainListResponse<BrainFinding> & { findings: BrainFinding[] };
export type RunsListResponse = BrainListResponse<BrainRun> & { runs: BrainRun[] };

// ============================================================================
// PATCH request bodies (sub-02/03/04 §11)
// ============================================================================

export interface DriftPatchBody {
  action: "dismiss" | "acknowledge" | "resolve";
  reason?: string;
  expires_when_signal_id_changes?: boolean;
}

export interface MemoryOpPatchBody {
  approval_state: "approved" | "dismissed" | "rejected";
  reason?: string; // required for dismiss/reject
  applied_artifact_ref?: string; // out-of-band link
}

export interface FindingPatchBody {
  approval_state: "approved" | "dismissed" | "resolved";
  reason?: string; // required for dismiss/reject
  closure_evidence?: EvidenceRef[]; // required for resolve
  applied_artifact_ref?: string;
}

export interface BulkPatchBody<T> {
  operation_ids?: string[]; // for memory ops
  finding_ids?: string[]; // for findings
  // shared transition fields:
  approval_state: T;
  reason?: string;
}

export interface RecomputeBody {
  cycle_key: string;
  sources?: Array<"digest" | "drift" | "memory_ops" | "learn">;
  force?: boolean;
  dry_run?: boolean;
}

// ============================================================================
// WebSocket payload — marvisx:brain_cycle_changed (sub-05 §4.16)
// ============================================================================

export interface BrainCycleChangedEvent {
  type: "marvisx:brain_cycle_changed";
  cycle_key: string;
  run_id: string;
  status: RunStatus;
  phase: "digest" | "journal" | "drift" | "memory_ops" | "learn" | "done";
  deltas: {
    events: number;
    drift: number;
    memory_ops: number;
    findings: number;
  };
}

// ============================================================================
// Pipeline subbar counters (sub-05 §4.4)
// ============================================================================

export interface PipelineCounters {
  ingest_pending: number; // from /inbox/files
  digest_events: number;
  journal_entries: number;
  drift_open: number;
  memory_ops_pending: number;
  findings_open: number;
}

// ============================================================================
// Daily landing aggregates (sub-05 §4.6)
// ============================================================================

export interface DailyLanding {
  cycle_key: string;
  run: BrainRun;
  blocks: {
    da_decidere: {
      total: number;
      preview: BrainFinding[]; // top N for landing
    };
    stride: {
      total: number;
      preview: DriftSignal[];
    };
    diario: {
      entry: JournalEntry; // company-scope entry for cycle
    };
  };
}

// ============================================================================
// Error response envelope (sub-05 §4.15)
// ============================================================================

export interface BrainErrorResponse {
  error_kind:
    | "lifecycle_conflict"
    | "cycle_not_found"
    | "cycle_too_old"
    | "cycle_too_large"
    | "already_applied"
    | "reason_required"
    | "bulk_cap_exceeded"
    | "invalid_idempotency_key"
    | "insufficient_role"
    | "redacted_unauthorized";
  message: string;
  details?: Record<string, unknown>;
}
