// MarvisX Console — Brain v1 surface helpers (sub-05 §2).
// Thin fetch wrappers used by the Brain pages. Returns the live envelopes
// emitted by /api/v1/brain/* — visibility-filtered server-side.

import { API_BASE_URL } from "@/lib/config";
import { BrainApiError, generateIdempotencyKey } from "./api";
import type {
  BrainFinding,
  BrainRun,
  DigestEvent,
  DriftSignal,
  JournalEntry,
  MemoryOperation,
} from "./types";

// Absolute URL so requests don't hit Next.js trailingSlash:true 308 redirect
// (which appends '/' → /api/v1/brain/runs/ → no FastAPI route → 404).
const BRAIN_API_BASE = `${API_BASE_URL}/api/v1/brain`;

/** @public */
export interface SurfaceListResponse<T> {
  items: T[];
  next_cursor?: string | null;
  cycle_key?: string | null;
  run_id?: string | null;
  redacted_count?: number;
  total_returned?: number;
  redacted_evidence_count?: number;
}

export interface PipelineCounters {
  cycle_key: string;
  run_id: string | null;
  ingest: number;
  digest: number;
  journal: number;
  drift: number;
  memory_ops: number;
  findings: number;
}

/** @public */
export interface BrainCapabilities {
  schema_version: number;
  event_types: string[];
  source_systems: string[];
  signal_types: string[];
  knowledge_forms: string[];
  operation_types: string[];
  finding_types: string[];
  severities: string[];
  confidence_tiers: string[];
  drift_axes: string[];
  approval_states: string[];
  finding_approval_states: string[];
  signal_states: string[];
  run_statuses: string[];
  run_triggers: string[];
  scope_types: string[];
  suggested_artifacts: string[];
  closure_condition_kinds: string[];
  knowledge_glyphs: Record<string, string>;
}

async function brainGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BRAIN_API_BASE}${path}`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new BrainApiError(res.status, null, path);
  return (await res.json()) as T;
}

async function brainSend<T>(
  method: "POST" | "PATCH",
  path: string,
  body: unknown,
  idempotencyKey: string | null,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const res = await fetch(`${BRAIN_API_BASE}${path}`, {
    method,
    credentials: "include",
    headers,
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw new BrainApiError(res.status, null, path);
  return (await res.json()) as T;
}

function listQs(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === "") continue;
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item != null && item !== "") search.append(k, String(item));
      }
    } else {
      search.set(k, String(v));
    }
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

// --- Surface endpoints ---

export async function fetchRuns(params: {
  cycle_key?: string;
  status?: string[];
  trigger?: string[];
  include_superseded?: boolean;
  limit?: number;
} = {}): Promise<SurfaceListResponse<BrainRun>> {
  return brainGet(`/runs${listQs(params)}`);
}

/** @public */
export async function fetchRun(run_id: string): Promise<BrainRun> {
  return brainGet(`/runs/${encodeURIComponent(run_id)}`);
}

export async function fetchCounters(
  cycle_key: string = "latest",
): Promise<PipelineCounters> {
  return brainGet(`/counters${listQs({ cycle_key })}`);
}

/** @public — deterministic narrative recap per scope (company + projects). */
export interface CycleRecapDecision {
  event_id: string;
  title: string;
  event_type: string | null;
  source_system: string | null;
  source_project: string | null;
}

export interface CycleRecapProjectBlock {
  scope_key: string;
  narrative: string;
  breakdown: Record<string, number>;
  decisions_count: number;
  decisions: CycleRecapDecision[];
}

export interface CycleRecap {
  cycle_key: string;
  resolved_cycle_key: string | null;
  company: {
    narrative: string;
    breakdown: Record<string, number>;
    decisions_count: number;
  };
  projects: CycleRecapProjectBlock[];
}

export async function fetchCycleRecap(
  cycle_key: string = "latest",
): Promise<CycleRecap> {
  return brainGet(`/cycles/${encodeURIComponent(cycle_key)}/recap`);
}

/** @public */
export async function fetchCapabilities(): Promise<BrainCapabilities> {
  return brainGet("/capabilities");
}

/** @public */
export async function fetchEvents(params: {
  cycle_key?: string;
  run_id?: string;
  event_type?: string[];
  source_project?: string;
  cursor?: string;
  limit?: number;
} = {}): Promise<SurfaceListResponse<DigestEvent>> {
  return brainGet(`/events${listQs(params)}`);
}

export async function fetchJournal(params: {
  cycle_key?: string;
  run_id?: string;
  scope_type?: string;
  scope_key?: string;
  program_key?: string;
  limit?: number;
} = {}): Promise<SurfaceListResponse<JournalEntry>> {
  return brainGet(`/journal${listQs(params)}`);
}

export async function fetchDrift(params: {
  cycle_key?: string;
  state?: string[];
  severity_min?: string;
  scope_type?: string;
  scope_key?: string;
  drift_axis?: string[];
  limit?: number;
} = {}): Promise<SurfaceListResponse<DriftSignal>> {
  return brainGet(`/drift${listQs(params)}`);
}

export async function fetchMemoryOps(params: {
  cycle_key?: string;
  approval_state?: string[];
  operation_type?: string[];
  include_terminal?: boolean;
  limit?: number;
} = {}): Promise<SurfaceListResponse<MemoryOperation>> {
  return brainGet(`/memory-operations${listQs(params)}`);
}

export async function fetchFindings(params: {
  cycle_key?: string;
  approval_state?: string[];
  finding_type?: string[];
  severity_min?: string;
  confidence_min?: string;
  recurrence_min?: number;
  regression_only?: boolean;
  limit?: number;
} = {}): Promise<SurfaceListResponse<BrainFinding>> {
  return brainGet(`/findings${listQs(params)}`);
}

/** @public */
export interface RecomputeResult {
  status: string;
  cycle_key: string;
  run_id: string | null;
  event_count: number;
  journal_count: number;
  duration_ms: number | null;
  mode: string | null;
  dry_run: boolean;
}

export async function recomputeCycle(
  cycle_key: string,
  opts: { sources?: string[]; force?: boolean; dry_run?: boolean; user_id: string } = { user_id: "anonymous" },
): Promise<RecomputeResult> {
  const key = generateIdempotencyKey(cycle_key, opts.user_id);
  return brainSend(
    "POST",
    `/cycles/${encodeURIComponent(cycle_key)}/recompute`,
    {
      sources: opts.sources ?? null,
      force: opts.force ?? false,
      dry_run: opts.dry_run ?? false,
    },
    key,
  );
}

/** @public */
export async function patchDriftSignal(
  signal_id: string,
  action: "dismiss" | "acknowledge" | "resolve" | "reopen",
  reason: string | null,
  user_id: string,
): Promise<DriftSignal> {
  const key = generateIdempotencyKey(signal_id, user_id, "drift");
  return brainSend("PATCH", `/drift/${encodeURIComponent(signal_id)}`, { action, reason }, key);
}

export async function patchMemoryOp(
  operation_id: string,
  approval_state: "approved" | "dismissed" | "rejected",
  reason: string | null,
  user_id: string,
): Promise<MemoryOperation> {
  const key = generateIdempotencyKey(operation_id, user_id, "memop");
  return brainSend(
    "PATCH",
    `/memory-operations/${encodeURIComponent(operation_id)}`,
    { approval_state, reason },
    key,
  );
}

export async function patchFinding(
  finding_id: string,
  approval_state: "approved" | "dismissed" | "resolved",
  reason: string | null,
  user_id: string,
): Promise<BrainFinding> {
  const key = generateIdempotencyKey(finding_id, user_id, "finding");
  return brainSend(
    "PATCH",
    `/findings/${encodeURIComponent(finding_id)}`,
    { approval_state, reason },
    key,
  );
}
