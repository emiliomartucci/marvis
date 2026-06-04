// MarvisX Console — Brain v1 API client
// Pre-implementation bozza. Da copiare in console/src/lib/brain/api.ts.
//
// 17 fetcher per gli endpoint sub-05 §2 + helper.
// Cross-cutting invariants:
// - Idempotency-Key obbligatoria su POST/PATCH.
// - cursor pagination con next_cursor opaque base64.
// - cycle_key="latest" risolto server-side.
// - Response envelope con redacted_count, total_estimate.
// - Visibility: 404 quando cross-project con un progetto invisibile.

import { API_BASE_URL } from "@/lib/config";
import type {
  ApplyGuidance,
  BrainErrorResponse,
  BrainFinding,
  BrainRun,
  BulkPatchBody,
  DigestListResponse,
  DriftListResponse,
  DriftPatchBody,
  DriftSignal,
  FindingPatchBody,
  FindingsListResponse,
  JournalEntry,
  JournalListResponse,
  MemoryOperation,
  MemoryOpPatchBody,
  MemoryOpsListResponse,
  RecomputeBody,
  RunsListResponse,
} from "./types";

// ============================================================================
// Base config
// ============================================================================

// Absolute URL: Next.js trailingSlash:true would 308-redirect relative paths
// to /api/v1/brain/runs/ which FastAPI does not match → 404.
const BRAIN_API_BASE = `${API_BASE_URL}/api/v1/brain`;

// Helper: generate Idempotency-Key from cycle + user + minute bucket
export function generateIdempotencyKey(
  cycle_key: string,
  user_id: string,
  prefix = "ui"
): string {
  const minute = Math.floor(Date.now() / 60000);
  return `${prefix}-${user_id}-${cycle_key}-${minute}`;
}

// Helper: fetch wrapper with consistent error handling
async function brainFetch<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const response = await fetch(`${BRAIN_API_BASE}${path}`, {
    credentials: "include",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let errorBody: BrainErrorResponse | null = null;
    try {
      errorBody = await response.json();
    } catch {
      // non-JSON error response
    }
    throw new BrainApiError(response.status, errorBody, path);
  }

  return response.json() as Promise<T>;
}

export class BrainApiError extends Error {
  constructor(
    public status: number,
    public body: BrainErrorResponse | null,
    public path: string
  ) {
    super(
      `Brain API ${path} failed: ${status} ${body?.error_kind ?? "unknown"}`
    );
    this.name = "BrainApiError";
  }
}

// ============================================================================
// 1-3: Runs + Recompute (sub-01 §6.D4)
// ============================================================================

type RunStatusFilter = "running" | "succeeded" | "partial" | "failed" | "superseded";
type ScopeTypeFilter = "company" | "program" | "project";

export interface GetRunsParams {
  latest?: boolean;
  status?: RunStatusFilter;
  scope_type?: ScopeTypeFilter;
  scope_key?: string;
  limit?: number;
  cursor?: string;
}

export async function getBrainRuns(
  params: GetRunsParams = {}
): Promise<RunsListResponse> {
  const qs = buildQueryString(params);
  return brainFetch<RunsListResponse>(`/runs${qs}`);
}

export async function getBrainRun(run_id: string): Promise<BrainRun> {
  return brainFetch<BrainRun>(`/runs/${encodeURIComponent(run_id)}`);
}

export async function recomputeBrainCycle(
  body: RecomputeBody,
  user_id: string
): Promise<BrainRun> {
  const cycle_key = body.cycle_key;
  return brainFetch<BrainRun>(
    `/cycles/${encodeURIComponent(cycle_key)}/recompute`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": generateIdempotencyKey(cycle_key, user_id, "recompute"),
      },
      body: JSON.stringify(body),
    }
  );
}

// ============================================================================
// 4: Digest events (sub-01 §6.D4 — NEW per sub-05 R2 / sub-01 §12 mandate)
// ============================================================================

export interface GetEventsParams {
  cycle_key?: string | "latest";
  event_type?: string; // comma-list
  source_project?: string;
  limit?: number;
  cursor?: string;
}

export async function getBrainEvents(
  params: GetEventsParams = {}
): Promise<DigestListResponse> {
  const qs = buildQueryString({ cycle_key: "latest", ...params });
  return brainFetch<DigestListResponse>(`/events${qs}`);
}

// ============================================================================
// 5: Journal (sub-01 §6.D2)
// ============================================================================

export interface GetJournalParams {
  cycle_key?: string | "latest";
  scope_type?: "company" | "program" | "project";
  scope_key?: string;
  limit?: number;
  cursor?: string;
}

export async function getBrainJournal(
  params: GetJournalParams = {}
): Promise<JournalListResponse> {
  const qs = buildQueryString({
    cycle_key: "latest",
    scope_type: "company",
    ...params,
  });
  return brainFetch<JournalListResponse>(`/journal${qs}`);
}

// ============================================================================
// 6-8: Drift signals (sub-02 §11.1)
// ============================================================================

export interface GetDriftParams {
  cycle_key?: string | "latest";
  scope_type?: "company" | "program" | "project";
  scope_key?: string;
  signal_type?: string;
  knowledge_form?: string;
  severity_min?: "low" | "medium" | "high" | "critical";
  confidence_min?: number;
  state?: string; // comma-list
  include_resolved?: boolean;
  created_after?: string;
  limit?: number;
  cursor?: string;
}

export async function getBrainDrift(
  params: GetDriftParams = {}
): Promise<DriftListResponse> {
  const qs = buildQueryString({
    cycle_key: "latest",
    state: "open",
    severity_min: "low",
    ...params,
  });
  return brainFetch<DriftListResponse>(`/drift${qs}`);
}

export async function getBrainDriftSignal(
  signal_id: string
): Promise<DriftSignal> {
  return brainFetch<DriftSignal>(`/drift/${encodeURIComponent(signal_id)}`);
}

export async function patchBrainDriftSignal(
  signal_id: string,
  body: DriftPatchBody,
  user_id: string,
  cycle_key: string
): Promise<DriftSignal> {
  return brainFetch<DriftSignal>(`/drift/${encodeURIComponent(signal_id)}`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": generateIdempotencyKey(cycle_key, user_id, `drift-${signal_id}`),
    },
    body: JSON.stringify(body),
  });
}

// ============================================================================
// 9-13: Memory operations (sub-03 §11.1)
// ============================================================================

export interface GetMemoryOpsParams {
  cycle_key?: string | "latest";
  scope_type?: "company" | "program" | "project" | "artifact";
  scope_key?: string;
  operation_type?: string; // comma-list of 6 active + 3 reserved
  approval_state?: string;
  score_min?: number;
  recurrence_min?: number;
  applied?: boolean;
  proposed_after?: string;
  limit?: number;
  cursor?: string;
}

export async function getBrainMemoryOperations(
  params: GetMemoryOpsParams = {}
): Promise<MemoryOpsListResponse> {
  const qs = buildQueryString({
    cycle_key: "latest",
    approval_state: "pending",
    recurrence_min: 1,
    ...params,
  });
  return brainFetch<MemoryOpsListResponse>(`/memory-operations${qs}`);
}

export async function getBrainMemoryOperation(
  operation_id: string
): Promise<MemoryOperation> {
  return brainFetch<MemoryOperation>(
    `/memory-operations/${encodeURIComponent(operation_id)}`
  );
}

export async function patchBrainMemoryOperation(
  operation_id: string,
  body: MemoryOpPatchBody,
  user_id: string,
  cycle_key: string
): Promise<MemoryOperation> {
  return brainFetch<MemoryOperation>(
    `/memory-operations/${encodeURIComponent(operation_id)}`,
    {
      method: "PATCH",
      headers: {
        "Idempotency-Key": generateIdempotencyKey(cycle_key, user_id, `memop-${operation_id}`),
      },
      body: JSON.stringify(body),
    }
  );
}

export async function bulkPatchBrainMemoryOperations(
  body: BulkPatchBody<MemoryOpPatchBody["approval_state"]>,
  user_id: string,
  cycle_key: string
): Promise<{ results: Array<{ operation_id: string; status: string; error?: string }> }> {
  const ids = body.operation_ids ?? [];
  if (ids.length > 25) {
    throw new Error(`Bulk cap exceeded: ${ids.length} > 25. Split into batches.`);
  }
  return brainFetch(`/memory-operations:bulk`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": generateIdempotencyKey(cycle_key, user_id, "bulk-memop"),
    },
    body: JSON.stringify(body),
  });
}

export async function applyBrainMemoryOperation(
  operation_id: string
): Promise<ApplyGuidance> {
  // GUIDANCE ONLY — does NOT write artifacts.
  return brainFetch<ApplyGuidance>(
    `/memory-operations/${encodeURIComponent(operation_id)}/apply`,
    { method: "POST" }
  );
}

// ============================================================================
// 14-18: Findings (sub-04 §11.1)
// ============================================================================

export interface GetFindingsParams {
  cycle_key?: string | "latest";
  scope_type?: "company" | "program" | "project" | "artifact";
  scope_key?: string;
  finding_type?: string;
  confidence_min?: "low" | "medium" | "high";
  recurrence_min?: number;
  approval_state?: string;
  include_resolved?: boolean;
  closure_state?: string;
  created_after?: string;
  limit?: number;
  cursor?: string;
}

export async function getBrainFindings(
  params: GetFindingsParams = {}
): Promise<FindingsListResponse> {
  const qs = buildQueryString({
    cycle_key: "latest",
    approval_state: "open",
    ...params,
  });
  return brainFetch<FindingsListResponse>(`/findings${qs}`);
}

export async function getBrainFinding(
  finding_id: string
): Promise<BrainFinding> {
  return brainFetch<BrainFinding>(`/findings/${encodeURIComponent(finding_id)}`);
}

export async function patchBrainFinding(
  finding_id: string,
  body: FindingPatchBody,
  user_id: string,
  cycle_key: string
): Promise<BrainFinding> {
  // CRITICAL invariant (sub-04 F1 / parent O4):
  // Approve does NOT auto-create artifact. Only marks approval_state.
  return brainFetch<BrainFinding>(`/findings/${encodeURIComponent(finding_id)}`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": generateIdempotencyKey(cycle_key, user_id, `finding-${finding_id}`),
    },
    body: JSON.stringify(body),
  });
}

export async function bulkPatchBrainFindings(
  body: BulkPatchBody<FindingPatchBody["approval_state"]>,
  user_id: string,
  cycle_key: string
): Promise<{ results: Array<{ finding_id: string; status: string; error?: string }> }> {
  const ids = body.finding_ids ?? [];
  if (ids.length > 25) {
    throw new Error(`Bulk cap exceeded: ${ids.length} > 25. Split into batches.`);
  }
  return brainFetch(`/findings:bulk`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": generateIdempotencyKey(cycle_key, user_id, "bulk-finding"),
    },
    body: JSON.stringify(body),
  });
}

export async function applyBrainFinding(
  finding_id: string
): Promise<ApplyGuidance> {
  // GUIDANCE ONLY — does NOT write artifacts.
  // Caller must invoke next_action.tool with `must_include_in_tags=brain_finding:{id}`.
  return brainFetch<ApplyGuidance>(
    `/findings/${encodeURIComponent(finding_id)}/apply`,
    { method: "POST" }
  );
}

// ============================================================================
// Helpers
// ============================================================================

function buildQueryString(params: object): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== null && v !== ""
  );
  if (entries.length === 0) return "";
  const qs = entries
    .map(
      ([k, v]) =>
        `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`
    )
    .join("&");
  return `?${qs}`;
}

// ============================================================================
// Bonus: aggregated helpers for UI surfaces
// ============================================================================

// PipelineSubbar counters (sub-05 §4.4)
// Note: ingest counter is from /inbox/files (existing route), not /brain
export async function getPipelineCounters(
  cycle_key: string | "latest" = "latest"
): Promise<{
  digest_events: number;
  journal_entries: number;
  drift_open: number;
  memory_ops_pending: number;
  findings_open: number;
}> {
  // Concurrent fetches; UI may render with partial data
  const [events, journal, drift, memop, findings] = await Promise.all([
    getBrainEvents({ cycle_key, limit: 1 }),
    getBrainJournal({ cycle_key, limit: 1 }),
    getBrainDrift({ cycle_key, state: "open", limit: 1 }),
    getBrainMemoryOperations({ cycle_key, approval_state: "pending", limit: 1 }),
    getBrainFindings({ cycle_key, approval_state: "open", limit: 1 }),
  ]);

  return {
    digest_events: events.total_estimate,
    journal_entries: journal.total_estimate,
    drift_open: drift.total_estimate,
    memory_ops_pending: memop.total_estimate,
    findings_open: findings.total_estimate,
  };
}

// Daily landing aggregate (sub-05 §4.6) — server endpoint TBD
// For now, compose client-side from 3 concurrent fetches
export async function getDailyLanding(
  cycle_key: string | "latest" = "latest",
  previewLimit = 5
): Promise<{
  cycle_key: string;
  run: BrainRun;
  blocks: {
    da_decidere: { total: number; preview: BrainFinding[] };
    stride: { total: number; preview: DriftSignal[] };
    diario: { entry: JournalEntry | null };
  };
}> {
  const [runs, findings, drift, journal] = await Promise.all([
    getBrainRuns({ latest: true, scope_type: "company" }),
    getBrainFindings({ cycle_key, approval_state: "open", limit: previewLimit }),
    getBrainDrift({ cycle_key, state: "open", severity_min: "medium", limit: previewLimit }),
    getBrainJournal({ cycle_key, scope_type: "company", limit: 1 }),
  ]);

  const run = runs.runs?.[0];
  if (!run) throw new Error("No succeeded run found");

  return {
    cycle_key: run.cycle_key,
    run,
    blocks: {
      da_decidere: {
        total: findings.total_estimate,
        preview: findings.findings ?? [],
      },
      stride: {
        total: drift.total_estimate,
        preview: drift.signals ?? [],
      },
      diario: {
        entry: journal.entries?.[0] ?? null,
      },
    },
  };
}
