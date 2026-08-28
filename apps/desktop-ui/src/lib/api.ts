import { API_BASE_URL } from "./config";
import type {
  CICheck,
  CIChecksSummary,
  SSOConfig,
  Workspace,
  WorkspaceInvite,
  SSOWorkspaceConfig,
  AuditLogResponse,
  CommentCreate,
  CommentResponse,
  CommentUpdate,
  ConversationCost,
  DocEntry,
  FinderFileContent,
  FinderListItem,
  FinderListResponse,
  FinderTreeNode,
  GitBranch,
  GitCommit,
  HandoffEntry,
  IngestDecisionResponse,
  IngestHistoryDecision,
  IngestHistoryEntry,
  IngestPendingItem,
  IngestPendingStatus,
  IngestSkipEntry,
  IngestSkipReason,
  IngestUploadResponse,
  LearningResponse,
  ManualProjectEdgeKind,
  ManualProjectEdgeWriteResponse,
  MergeConflictResponse,
  InboxItemDetail,
  InboxItemSummary,
  Notification,
  ProgramInfo,
  ProjectBillingSummary,
  ProjectCostSummary,
  ProjectDetail,
  PullRequest,
  RaciEntry,
  RaciRole,
  ReactionType,
  SessionCatalogResponse,
  Session,
  SessionProvider,
  StatusUpdateCreate,
  StatusUpdateResponse,
  TaskCreateRequest,
  TaskCostSummary,
  TaskResponse,
  TaskUpdateRequest,
  TargetType,
  Team,
  TeamMember,
  TeamProject,
  TerminalNetworkProbeResponse,
  TerminalMetricsSnapshot,
  TicketResponse,
  User,
  UserCreateRequest,
  UserInfo,
  MonitoringSnapshot,
  SecurityData,
  DiskTreeResponse,
  CandleDatapoint,
} from "./types";

type InboxDigestRow = {
  inbox_item_id: string;
  domain_key: string;
  title: string | null;
  url: string | null;
  topic: InboxItemSummary["topic"];
  treatment: InboxItemSummary["treatment"];
  status: InboxItemSummary["status"];
  created_at: string | null;
};

type InboxDigestStats = {
  cycle_key: string;
  visible: number;
  overflow: number;
  expired: number;
};

export class APIError extends Error {
  status: number;
  detail: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.detail = detail;
  }
}

type FetchAPIOptions = RequestInit & {
  redirectOnUnauthorized?: boolean;
};

function responseErrorMessage(
  detail: unknown,
  status: number,
  unauthorizedMessage?: string
): string {
  if (typeof detail === "string") return detail;
  if (
    detail &&
    typeof detail === "object" &&
    "message" in detail &&
    typeof detail.message === "string"
  ) {
    return detail.message;
  }
  if (status === 401 && unauthorizedMessage) return unauthorizedMessage;
  return `HTTP ${status}`;
}

function withQuery(path: string, query: string): string {
  return query ? `${path}?${query}` : path;
}

async function fetchAPI<T>(
  path: string,
  options: FetchAPIOptions = {}
): Promise<T> {
  const { redirectOnUnauthorized = true, ...fetchOptions } = options;
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...fetchOptions,
    credentials: "include",
    signal: fetchOptions.signal,
    headers: {
      "Content-Type": "application/json",
      ...fetchOptions.headers,
    },
  });

  if (res.status === 401) {
    if (redirectOnUnauthorized && typeof window !== "undefined") {
      window.location.href = "/login/";
    }
    throw new APIError("Unauthorized", 401);
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new APIError(responseErrorMessage(body.detail, res.status), res.status, body.detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

/**
 * Wrapper around fetchAPI that validates the response with a Zod schema.
 * Throws a typed error if parsing fails.
 */
export async function fetchAPIValidated<T>(
  path: string,
  schema: { parse: (data: unknown) => T },
  options: RequestInit = {}
): Promise<T> {
  const raw = await fetchAPI<unknown>(path, options);
  return schema.parse(raw);
}

export async function login(email: string, password: string): Promise<void> {
  // Bypass fetchAPI's 401 intercept so the login page can show the error directly
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(responseErrorMessage(body.detail, res.status, "Invalid credentials"));
  }
}

export async function getMe(): Promise<UserInfo> {
  return fetchAPI<UserInfo>("/api/v1/auth/me");
}

export async function logout(): Promise<void> {
  await fetchAPI("/api/v1/auth/logout", { method: "POST" });
}

export async function listSessions(): Promise<Session[]> {
  return fetchAPI<Session[]>("/api/v1/sessions");
}

export async function createSession(
  input: {
    name: string;
    project_slug?: string;
    provider?: SessionProvider;
    model?: string;
    permission_preset?: string;
    theme_mode?: "light" | "dark";
  },
): Promise<Session> {
  const body: Record<string, string> = { name: input.name };
  if (input.project_slug) body.project_slug = input.project_slug;
  if (input.provider) body.provider = input.provider;
  if (input.model !== undefined) body.model = input.model;
  if (input.permission_preset) body.permission_preset = input.permission_preset;
  if (input.theme_mode) body.theme_mode = input.theme_mode;
  return fetchAPI<Session>("/api/v1/sessions", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getSessionCatalog(
  opts?: { signal?: AbortSignal },
): Promise<SessionCatalogResponse> {
  return fetchAPI<SessionCatalogResponse>("/api/v1/sessions/catalog", { signal: opts?.signal });
}

export async function completeSession(name: string): Promise<Session> {
  return fetchAPI<Session>(
    `/api/v1/sessions/${encodeURIComponent(name)}/complete`,
    { method: "POST" }
  );
}

export async function deleteSession(name: string): Promise<void> {
  await fetchAPI(`/api/v1/sessions/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export async function updateSession(
  name: string,
  updates: {
    new_name?: string;
    display_name?: string;
    pinned?: boolean;
    group_name?: string;
    project_slug?: string | null;
    agent_managed?: boolean;
  }
): Promise<Session> {
  return fetchAPI<Session>(
    `/api/v1/sessions/${encodeURIComponent(name)}`,
    {
      method: "PATCH",
      body: JSON.stringify(updates),
    }
  );
}

export async function reorderSessions(order: string[]): Promise<Session[]> {
  return fetchAPI<Session[]>("/api/v1/sessions/reorder", {
    method: "PUT",
    body: JSON.stringify({ order }),
  });
}

export async function sendSessionMessage(
  name: string,
  text: string
): Promise<void> {
  await fetchAPI(`/api/v1/sessions/${encodeURIComponent(name)}/send-message`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export async function resurrectSession(name: string): Promise<Session> {
  return fetchAPI<Session>(
    `/api/v1/sessions/${encodeURIComponent(name)}/resurrect`,
    { method: "POST" }
  );
}

export async function getTerminalTicket(
  sessionName: string
): Promise<TicketResponse> {
  return fetchAPI<TicketResponse>("/api/v1/terminal/ticket", {
    method: "POST",
    body: JSON.stringify({ session_name: sessionName }),
  });
}

export async function getTerminalMetrics(opts?: { signal?: AbortSignal }): Promise<TerminalMetricsSnapshot> {
  return fetchAPI<TerminalMetricsSnapshot>("/api/v1/terminal/metrics", {
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function postTerminalMetricsBatch(
  batch: unknown,
  opts?: { signal?: AbortSignal },
): Promise<void> {
  await fetchAPI<void>("/api/v1/terminal/metrics-batch", {
    method: "POST",
    body: JSON.stringify(batch),
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function getTerminalNetworkProbe(
  opts?: { signal?: AbortSignal; bytes?: number },
): Promise<TerminalNetworkProbeResponse> {
  const bytes = opts?.bytes ?? 65_536;
  return fetchAPI<TerminalNetworkProbeResponse>(
    `/api/v1/terminal/network-probe?bytes=${encodeURIComponent(bytes)}`,
    {
      signal: opts?.signal,
      redirectOnUnauthorized: false,
    },
  );
}

// --- Projects ---

export async function getPrograms(opts?: { signal?: AbortSignal }): Promise<ProgramInfo[]> {
  return fetchAPI<ProgramInfo[]>("/api/v1/projects", { signal: opts?.signal });
}

export async function getProjectDetail(
  slug: string,
  opts?: { signal?: AbortSignal; deep?: boolean },
): Promise<ProjectDetail> {
  const params = new URLSearchParams();
  if (opts?.deep !== undefined) params.set("deep", String(opts.deep));
  const qs = params.toString();
  return fetchAPI<ProjectDetail>(
    withQuery(`/api/v1/projects/${encodeURIComponent(slug)}`, qs),
    { signal: opts?.signal },
  );
}

export async function updateProjectColor(
  slug: string,
  color: string | null,
  opts?: { signal?: AbortSignal },
): Promise<ProjectDetail> {
  return fetchAPI<ProjectDetail>(`/api/v1/projects/${encodeURIComponent(slug)}`, {
    method: "PATCH",
    body: JSON.stringify({ color }),
    signal: opts?.signal,
  });
}

export async function getProjectHandoffs(slug: string, opts?: { signal?: AbortSignal }): Promise<HandoffEntry[]> {
  return fetchAPI<HandoffEntry[]>(`/api/v1/projects/${encodeURIComponent(slug)}/handoffs`, { signal: opts?.signal });
}

/**
 * Fetch every doc under `docs/*` for the given project (plans, brainstorms,
 * solutions, audits, research, guides, rubrics, analysis, briefs, spikes).
 *
 * The backend endpoint is historically named `/plans` but iterates the full
 * `docs/` tree (see `api/routers/projects.py::project_plans`). We expose the
 * correct client-side name here and leave the endpoint path untouched to
 * avoid breaking external API consumers.
 */
export async function getProjectDocs(slug: string, opts?: { signal?: AbortSignal }): Promise<DocEntry[]> {
  return fetchAPI<DocEntry[]>(`/api/v1/projects/${encodeURIComponent(slug)}/plans`, { signal: opts?.signal });
}

export async function getProjectGitLog(slug: string, limit = 20, opts?: { signal?: AbortSignal }): Promise<GitCommit[]> {
  return fetchAPI<GitCommit[]>(`/api/v1/projects/${encodeURIComponent(slug)}/git/log?limit=${limit}`, { signal: opts?.signal });
}

export async function getProjectGitDiff(slug: string, opts?: { signal?: AbortSignal }): Promise<{ diff: string }> {
  return fetchAPI<{ diff: string }>(`/api/v1/projects/${encodeURIComponent(slug)}/git/diff`, { signal: opts?.signal });
}

export async function getProjectGitBranches(slug: string, opts?: { signal?: AbortSignal }): Promise<GitBranch[]> {
  return fetchAPI<GitBranch[]>(`/api/v1/projects/${encodeURIComponent(slug)}/git/branches`, { signal: opts?.signal });
}

export async function upsertManualProjectEdge(
  data: { src_slug: string; dst_slug: string; kind: ManualProjectEdgeKind },
  opts?: { signal?: AbortSignal },
): Promise<ManualProjectEdgeWriteResponse> {
  return fetchAPI<ManualProjectEdgeWriteResponse>("/api/v1/kg/edges/manual", {
    method: "POST",
    body: JSON.stringify(data),
    signal: opts?.signal,
  });
}

export async function deleteManualProjectEdge(
  data: { src_slug: string; dst_slug: string; kind: ManualProjectEdgeKind },
  opts?: { signal?: AbortSignal },
): Promise<ManualProjectEdgeWriteResponse> {
  return fetchAPI<ManualProjectEdgeWriteResponse>("/api/v1/kg/edges/manual", {
    method: "DELETE",
    body: JSON.stringify(data),
    signal: opts?.signal,
  });
}

export async function listLearnings(
  params?: {
    project?: string;
    category?: string;
    severity?: string;
    tags?: string;
    search?: string;
    module?: string;
    limit?: number;
    offset?: number;
    deep?: boolean;
  },
  opts?: { signal?: AbortSignal },
): Promise<LearningResponse[]> {
  const searchParams = new URLSearchParams();
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        searchParams.set(key, String(value));
      }
    }
  }
  const qs = searchParams.toString();
  return fetchAPI<LearningResponse[]>(withQuery("/api/v1/learnings", qs), {
    signal: opts?.signal,
  });
}

// --- Inbox ---

export async function getInboxItems(
  opts?: {
    needsTriage?: boolean;
    limit?: number;
    source?: string;
    program?: string;
    topic?: string;
    treatment?: string;
    status?: string;
    signal?: AbortSignal;
  }
): Promise<InboxItemSummary[]> {
  const params = new URLSearchParams();
  params.set("needs_triage", String(opts?.needsTriage ?? true));
  params.set("limit", String(opts?.limit ?? 50));
  if (opts?.source) params.set("source", opts.source);
  if (opts?.program) params.set("program", opts.program);
  if (opts?.topic) params.set("topic", opts.topic);
  if (opts?.treatment) params.set("treatment", opts.treatment);
  if (opts?.status) params.set("status", opts.status);
  return fetchAPI<InboxItemSummary[]>(`/api/v1/inbox/items?${params.toString()}`, {
    signal: opts?.signal,
  });
}

export async function patchInboxItemStatus(
  inboxItemId: string,
  body: { status: string; ignore_reason?: string },
  opts?: { signal?: AbortSignal }
): Promise<{ inbox_item_id: string; status: string; ignore_reason: string | null; decided_by: string; decided_at: string }> {
  return fetchAPI(`/api/v1/inbox/items/${encodeURIComponent(inboxItemId)}/status`, {
    method: "PATCH",
    body: JSON.stringify(body),
    signal: opts?.signal,
  });
}

export async function getInboxUnreadCount(
  opts?: { signal?: AbortSignal }
): Promise<{ count: number }> {
  return fetchAPI<{ count: number }>("/api/v1/inbox/items/unread-count", {
    signal: opts?.signal,
  });
}

function normalizeDigestRow(raw: InboxDigestRow): InboxItemSummary {
  return {
    id: raw.inbox_item_id,
    source_type: raw.domain_key,
    source_label: raw.domain_key,
    external_id: null,
    title: raw.title,
    snippet: null,
    sender: null,
    url: raw.url,
    program: null,
    project: null,
    topic: raw.topic,
    treatment: raw.treatment,
    status: raw.status,
    ignore_reason: null,
    received_at: raw.created_at,
    needs_triage: raw.status === "unread",
    triage: null,
  };
}

export async function getInboxDigestItems(
  opts?: { limit?: number; signal?: AbortSignal }
): Promise<InboxItemSummary[]> {
  const params = new URLSearchParams();
  params.set("limit", String(opts?.limit ?? 50));
  const raw = await fetchAPI<InboxDigestRow[]>(`/api/v1/inbox/digest/current?${params.toString()}`, {
    signal: opts?.signal,
  });
  return raw.map(normalizeDigestRow);
}

export async function getInboxDigestStats(
  opts?: { signal?: AbortSignal }
): Promise<InboxDigestStats> {
  return fetchAPI<InboxDigestStats>("/api/v1/inbox/digest/stats", {
    signal: opts?.signal,
  });
}

export async function getInboxStats(
  opts?: {
    needsTriage?: boolean;
    source?: string;
    program?: string;
    topic?: string;
    treatment?: string;
    signal?: AbortSignal;
  }
): Promise<import("./types").InboxStats> {
  const params = new URLSearchParams();
  params.set("needs_triage", String(opts?.needsTriage ?? true));
  if (opts?.source) params.set("source", opts.source);
  if (opts?.program) params.set("program", opts.program);
  if (opts?.topic) params.set("topic", opts.topic);
  if (opts?.treatment) params.set("treatment", opts.treatment);
  return fetchAPI(`/api/v1/inbox/stats?${params.toString()}`, {
    signal: opts?.signal,
  });
}

export async function getInboxItem(
  inboxItemId: string,
  opts?: { signal?: AbortSignal }
): Promise<InboxItemDetail> {
  return fetchAPI<InboxItemDetail>(`/api/v1/inbox/items/${encodeURIComponent(inboxItemId)}`, {
    signal: opts?.signal,
  });
}

// --- File ingest triage ---

export async function listIngestPending(
  opts?: {
    status?: IngestPendingStatus;
    projectSlug?: string;
    limit?: number;
    signal?: AbortSignal;
  }
): Promise<IngestPendingItem[]> {
  const params = new URLSearchParams();
  if (opts?.status) params.set("status", opts.status);
  if (opts?.projectSlug) params.set("project_slug", opts.projectSlug);
  params.set("limit", String(opts?.limit ?? 100));
  return fetchAPI<IngestPendingItem[]>(`/api/v1/ingest/pending?${params.toString()}`, {
    signal: opts?.signal,
  });
}

// UX-6: list silently-skipped upload entries (dedup / invalid / mime).
// Powers the "Ignorati" sidebar group in PendingList.
export async function listIngestSkipped(
  opts?: {
    projectSlug?: string;
    reason?: IngestSkipReason;
    limit?: number;
    signal?: AbortSignal;
  }
): Promise<IngestSkipEntry[]> {
  const params = new URLSearchParams();
  if (opts?.projectSlug) params.set("project_slug", opts.projectSlug);
  if (opts?.reason) params.set("reason", opts.reason);
  params.set("limit", String(opts?.limit ?? 100));
  return fetchAPI<IngestSkipEntry[]>(`/api/v1/ingest/skipped?${params.toString()}`, {
    signal: opts?.signal,
  });
}

export async function listIngestHistory(
  opts?: {
    decision?: IngestHistoryDecision | "all";
    today?: boolean;
    projectSlug?: string;
    limit?: number;
    signal?: AbortSignal;
  }
): Promise<IngestHistoryEntry[]> {
  const params = new URLSearchParams();
  params.set("decision", opts?.decision ?? "all");
  params.set("today", String(opts?.today ?? true));
  if (opts?.projectSlug) params.set("project_slug", opts.projectSlug);
  params.set("limit", String(opts?.limit ?? 80));
  return fetchAPI<IngestHistoryEntry[]>(`/api/v1/ingest/history?${params.toString()}`, {
    signal: opts?.signal,
  });
}

async function uploadIngestForm(
  path: string,
  formData: FormData,
  opts?: { signal?: AbortSignal }
): Promise<IngestUploadResponse> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    credentials: "include",
    signal: opts?.signal,
    body: formData,
  });

  if (res.status === 401) {
    if (typeof window !== "undefined") window.location.href = "/login/";
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(responseErrorMessage(body.detail, res.status));
  }

  return res.json();
}

export async function uploadIngestFolder(
  projectSlug: string,
  files: Array<{ file: File; relativePath: string }>,
  opts?: { signal?: AbortSignal }
): Promise<IngestUploadResponse> {
  const formData = new FormData();
  formData.set("project_slug", projectSlug);
  for (const item of files) {
    formData.append("files", item.file, item.relativePath);
  }
  return uploadIngestForm("/api/v1/ingest/upload-folder", formData, opts);
}

export async function uploadIngestZip(
  projectSlug: string,
  archive: File,
  opts?: { signal?: AbortSignal }
): Promise<IngestUploadResponse> {
  const formData = new FormData();
  formData.set("project_slug", projectSlug);
  formData.set("archive", archive, archive.name);
  return uploadIngestForm("/api/v1/ingest/upload-zip", formData, opts);
}

export async function approveIngestPending(
  id: string,
  opts?: { signal?: AbortSignal }
): Promise<IngestDecisionResponse> {
  return fetchAPI<IngestDecisionResponse>(
    `/api/v1/ingest/pending/${encodeURIComponent(id)}/approve`,
    { method: "POST", signal: opts?.signal }
  );
}

export interface IngestPendingPatchBody {
  project_slug?: string;
}

export async function patchIngestPending(
  id: string,
  patch: IngestPendingPatchBody,
  opts?: { ifMatch?: string; signal?: AbortSignal }
): Promise<IngestPendingItem> {
  const headers: Record<string, string> = {};
  if (opts?.ifMatch) headers["If-Match"] = opts.ifMatch;
  return fetchAPI<IngestPendingItem>(
    `/api/v1/ingest/pending/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      headers,
      body: JSON.stringify(patch),
      signal: opts?.signal,
    }
  );
}

export async function rejectIngestPending(
  id: string,
  opts?: { signal?: AbortSignal }
): Promise<IngestDecisionResponse> {
  return fetchAPI<IngestDecisionResponse>(
    `/api/v1/ingest/pending/${encodeURIComponent(id)}/reject`,
    { method: "POST", signal: opts?.signal }
  );
}

export async function deleteIngestPending(
  id: string,
  opts?: { signal?: AbortSignal }
): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/api/v1/ingest/pending/${encodeURIComponent(id)}`,
    { method: "DELETE", credentials: "include", signal: opts?.signal }
  );
  if (!res.ok && res.status !== 204) {
    throw new APIError(`Delete failed: HTTP ${res.status}`, res.status);
  }
}

// UX-1: pre-upload dedup probe. Returns the existing non-rejected row for
// (sha256, project_slug) — null/undefined if no collision.
export interface IngestPreflightResult {
  exists: boolean;
  id?: string;
  status?: string;
  file_path?: string;
}

export async function preflightIngest(
  sha256: string,
  projectSlug: string,
  opts?: { signal?: AbortSignal }
): Promise<IngestPreflightResult> {
  const params = new URLSearchParams({ sha256, project_slug: projectSlug });
  return fetchAPI<IngestPreflightResult>(
    `/api/v1/ingest/preflight?${params.toString()}`,
    { signal: opts?.signal }
  );
}

export async function retryParseIngestPending(
  id: string,
  opts?: { signal?: AbortSignal }
): Promise<IngestDecisionResponse> {
  return fetchAPI<IngestDecisionResponse>(
    `/api/v1/ingest/pending/${encodeURIComponent(id)}/reparse`,
    { method: "POST", signal: opts?.signal }
  );
}

export async function getIngestPreviewMd(
  id: string,
  opts?: { signal?: AbortSignal }
): Promise<string> {
  const res = await fetch(
    `${API_BASE_URL}/api/v1/ingest/pending/${encodeURIComponent(id)}/preview.md`,
    {
      credentials: "include",
      signal: opts?.signal,
    }
  );
  if (res.status === 401) {
    if (typeof window !== "undefined") window.location.href = "/login/";
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.detail;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
  }
  return res.text();
}

export async function getIngestPreviewBlob(
  id: string,
  ext: "pdf" | "xlsx" | "image",
  opts?: { signal?: AbortSignal }
): Promise<Blob> {
  const res = await fetch(
    `${API_BASE_URL}/api/v1/ingest/pending/${encodeURIComponent(id)}/preview.${ext}`,
    {
      credentials: "include",
      signal: opts?.signal,
    }
  );
  if (res.status === 401) {
    if (typeof window !== "undefined") window.location.href = "/login/";
    throw new APIError("Unauthorized", 401);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.detail;
    throw new APIError(
      typeof detail === "string" ? detail : `HTTP ${res.status}`,
      res.status,
      detail
    );
  }
  return res.blob();
}

export function getIngestPreviewUrl(
  id: string,
  ext: "pdf" | "xlsx" | "image" | "md"
): string {
  return `${API_BASE_URL}/api/v1/ingest/pending/${encodeURIComponent(id)}/preview.${ext}`;
}

export interface TriageCounters {
  auto: number;
  manual: number;
}

export async function fetchTriageCounters(
  opts?: { today?: boolean; signal?: AbortSignal }
): Promise<TriageCounters> {
  const today = opts?.today ?? true;
  return fetchAPI<TriageCounters>(
    `/api/v1/ingest/counters?today=${today}`,
    { signal: opts?.signal }
  );
}

/**
 * @deprecated Use fetchTriageCounters() instead. Retained as backward-compat wrapper.
 */
export async function fetchAutoApprovedCount(
  opts?: { today?: boolean; signal?: AbortSignal }
): Promise<number> {
  const counters = await fetchTriageCounters(opts);
  return counters.auto;
}

export async function generateTldr(
  inboxItemId: string,
  opts?: { signal?: AbortSignal }
): Promise<{ tldr: string; cached: boolean }> {
  return fetchAPI<{ tldr: string; cached: boolean }>(
    `/api/v1/inbox/items/${encodeURIComponent(inboxItemId)}/tldr`,
    { method: "POST", signal: opts?.signal }
  );
}

export async function generateDeepResearch(
  inboxItemId: string,
  opts?: { signal?: AbortSignal }
): Promise<{ deep_research: string; cached: boolean }> {
  return fetchAPI<{ deep_research: string; cached: boolean }>(
    `/api/v1/inbox/items/${encodeURIComponent(inboxItemId)}/deep-research`,
    { method: "POST", signal: opts?.signal }
  );
}

export interface SourceScore {
  source_key: string;
  score: number;
  upvotes: number;
  downvotes: number;
}

export async function getSourceScores(
  opts?: { signal?: AbortSignal }
): Promise<SourceScore[]> {
  return fetchAPI<SourceScore[]>("/api/v1/inbox/source-scores", {
    signal: opts?.signal,
  });
}

export async function projectGitPush(slug: string): Promise<{ success: boolean; error?: string }> {
  return fetchAPI(`/api/v1/projects/${encodeURIComponent(slug)}/git/push`, { method: "POST" });
}

export async function projectGitPull(slug: string): Promise<{ success: boolean; error?: string }> {
  return fetchAPI(`/api/v1/projects/${encodeURIComponent(slug)}/git/pull`, { method: "POST" });
}

export async function getProjectGitGraph(
  slug: string, limit = 50, skip = 0, allBranches = true,
  opts?: { signal?: AbortSignal }
): Promise<import("./types").GitGraphResponse> {
  const params = new URLSearchParams({
    limit: String(limit), skip: String(skip), all_branches: String(allBranches),
  });
  return fetchAPI(`/api/v1/projects/${encodeURIComponent(slug)}/git/graph?${params}`, { signal: opts?.signal });
}

export async function getGitCommitDetail(
  slug: string, hash: string, opts?: { signal?: AbortSignal }
): Promise<import("./types").GitCommitDetail> {
  return fetchAPI(`/api/v1/projects/${encodeURIComponent(slug)}/git/commit/${encodeURIComponent(hash)}`, { signal: opts?.signal });
}

// --- Status Updates ---

export async function createStatusUpdate(data: StatusUpdateCreate): Promise<StatusUpdateResponse> {
  return fetchAPI<StatusUpdateResponse>("/api/v1/status-updates", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function getStatusUpdates(project: string, opts?: { signal?: AbortSignal }): Promise<StatusUpdateResponse[]> {
  return fetchAPI<StatusUpdateResponse[]>(`/api/v1/status-updates?project=${encodeURIComponent(project)}`, { signal: opts?.signal });
}

export async function getOverdueProjects(opts?: { signal?: AbortSignal }): Promise<StatusUpdateResponse[]> {
  return fetchAPI<StatusUpdateResponse[]>("/api/v1/status-updates/overdue", { signal: opts?.signal });
}

// --- Project status updates feed (PR #9 single-pager v2) ---

export async function getProjectStatusUpdates(
  slug: string,
  limit = 20,
  opts?: { signal?: AbortSignal }
): Promise<import("./types").StatusUpdateFeedResponse> {
  return fetchAPI<import("./types").StatusUpdateFeedResponse>(
    `/api/v1/projects/${encodeURIComponent(slug)}/status-updates?limit=${limit}`,
    { signal: opts?.signal }
  );
}

export async function postProjectStatusUpdate(
  slug: string,
  content_md: string
): Promise<import("./types").StatusUpdateFeedItem> {
  return fetchAPI<import("./types").StatusUpdateFeedItem>(
    `/api/v1/projects/${encodeURIComponent(slug)}/status-updates`,
    {
      method: "POST",
      body: JSON.stringify({ content_md }),
    }
  );
}

// --- Comments ---

export async function createComment(data: CommentCreate): Promise<CommentResponse> {
  return fetchAPI<CommentResponse>("/api/v1/comments", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function getComments(targetType: TargetType, targetId: string, opts?: { signal?: AbortSignal }): Promise<CommentResponse[]> {
  return fetchAPI<CommentResponse[]>(
    `/api/v1/comments?target_type=${encodeURIComponent(targetType)}&target_id=${encodeURIComponent(targetId)}`,
    { signal: opts?.signal }
  );
}

export async function updateComment(id: number, data: CommentUpdate): Promise<CommentResponse> {
  return fetchAPI<CommentResponse>(`/api/v1/comments/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteComment(id: number): Promise<void> {
  await fetchAPI(`/api/v1/comments/${id}`, { method: "DELETE" });
}

export async function addReaction(commentId: number, reaction: ReactionType): Promise<void> {
  await fetchAPI(`/api/v1/comments/${commentId}/reactions`, {
    method: "POST",
    body: JSON.stringify({ reaction }),
  });
}

export async function removeReaction(commentId: number, reaction: ReactionType): Promise<void> {
  await fetchAPI(`/api/v1/comments/${commentId}/reactions/${encodeURIComponent(reaction)}`, {
    method: "DELETE",
  });
}

// --- Tasks (Triage) ---

export async function listTasks(
  params?: {
    project?: string;
    status?: string;
    kind?: string;
    priority?: string;
    created_by?: string;
    owner_id?: string;
    delegation?: string;
    tags?: string;
    sort?: string;
    limit?: number;
    offset?: number;
    include_deleted?: boolean;
    detailed?: boolean;
    deep?: boolean;
  },
  opts?: { signal?: AbortSignal }
): Promise<TaskResponse[]> {
  const searchParams = new URLSearchParams();
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        searchParams.set(key, String(value));
      }
    }
  }
  const qs = searchParams.toString();
  return fetchAPI<TaskResponse[]>(withQuery("/api/v1/tasks", qs), {
    signal: opts?.signal,
  });
}

export async function createTask(
  data: TaskCreateRequest,
  opts?: { signal?: AbortSignal }
): Promise<TaskResponse> {
  return fetchAPI<TaskResponse>("/api/v1/tasks", {
    method: "POST",
    body: JSON.stringify(data),
    signal: opts?.signal,
  });
}

export async function getTask(
  taskId: string,
  opts?: { signal?: AbortSignal }
): Promise<TaskResponse> {
  return fetchAPI<TaskResponse>(`/api/v1/tasks/${encodeURIComponent(taskId)}`, {
    signal: opts?.signal,
  });
}

export async function updateTask(
  taskId: string,
  data: TaskUpdateRequest,
  opts?: { signal?: AbortSignal }
): Promise<TaskResponse> {
  return fetchAPI<TaskResponse>(
    `/api/v1/tasks/${encodeURIComponent(taskId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(data),
      signal: opts?.signal,
    }
  );
}

export async function deleteTask(
  taskId: string,
  opts?: { signal?: AbortSignal }
): Promise<void> {
  await fetchAPI(`/api/v1/tasks/${encodeURIComponent(taskId)}`, {
    method: "DELETE",
    signal: opts?.signal,
  });
}

// --- Brain Diario ---

export type BrainRunStatus =
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "superseded";

export interface BrainRunResponse {
  run_id: string;
  workspace_id: string;
  cycle_key: string;
  cycle_window_start_utc: string;
  cycle_window_end_utc: string;
  cutoff_hour_utc_at_run: number;
  scope_type: "company";
  scope_key: string;
  trigger: "batch" | "manual" | "backfill";
  triggered_by?: string | null;
  started_at: string;
  finished_at?: string | null;
  status: BrainRunStatus;
  superseded_by_run_id?: string | null;
  event_count: number;
  partial_failures: Array<Record<string, unknown>>;
  duration_ms?: number | null;
  error_summary?: string | null;
}

export interface BrainRunsListResponse {
  items: BrainRunResponse[];
  next_cursor?: string | null;
  cycle_key?: string | null;
  total_returned?: number;
}

export interface BrainJournalBodyResponse {
  what_changed: Array<Record<string, unknown>>;
  decisions_observed: string[];
  open_loops: Array<Record<string, unknown>>;
  notable_context: Array<Record<string, unknown>>;
  sources: string[];
  tomorrow_watch: Array<Record<string, unknown>>;
}

export interface BrainJournalEntryResponse {
  entry_id: string;
  run_id: string;
  workspace_id: string;
  cycle_key: string;
  scope_type: "company" | "program" | "project";
  scope_key: string;
  program_key?: string | null;
  body: BrainJournalBodyResponse;
  is_empty: boolean;
  published_at: string;
  redacted_count?: number;
  narrative_polished?: string | null;
  cited_evidence_refs?: string[] | null;
  polish_model?: string | null;
}

export interface BrainJournalListResponse {
  items: BrainJournalEntryResponse[];
  next_cursor?: string | null;
  cycle_key?: string | null;
  run_id?: string | null;
  total_returned?: number;
}

export interface BrainRunTriggerResponse {
  started: boolean;
}

export interface TodoCreateRequestLocal {
  text: string;
  type?: "promemoria" | "azione" | "idea" | "decidi" | "rivedi";
  project?: string | null;
  fu?: string | null;
  payload?: Record<string, unknown> | null;
  source?: "user" | "agent" | "brain";
  source_ref?: string | null;
  doer?: TodoDoer | null;
}

type TodoDoer = "human" | "agent" | "hybrid";

export interface TodoDelegateRequestLocal {
  title?: string | null;
  project?: string | null;
}

export interface TodoResponseLocal {
  id: string;
  type: "promemoria" | "azione" | "idea" | "decidi" | "approva" | "rivedi";
  family: "captured" | "system";
  status: string;
  text: string;
  payload?: Record<string, unknown> | null;
  fu: string;
  project?: string | null;
  source: string;
  source_ref?: string | null;
  doer?: "human" | "agent" | "hybrid" | null;
  linked_task_id?: string | null;
  created_at: string;
  updated_at: string;
  resolved_at?: string | null;
  virtual?: boolean;
  origin?: {
    kind: "task_review" | "finding" | "memory_op";
    id: string;
  } | null;
}

export interface TodoListParamsLocal extends Record<string, unknown> {
  status?: string;
  type?: string;
  project?: string;
  limit?: number;
  offset?: number;
}

export interface TodoUpdateRequestLocal {
  text?: string;
  type?: "promemoria" | "azione" | "idea" | "decidi" | "rivedi";
  status?: string;
  project?: string | null;
  fu?: string | null;
  payload?: Record<string, unknown> | null;
  doer?: "human" | "agent" | "hybrid" | null;
}

export type VirtualTodoActionLocal = "approve" | "reject";

function appendListParam(searchParams: URLSearchParams, key: string, value: unknown): void {
  if (value === undefined || value === null || value === "") return;
  if (Array.isArray(value)) {
    for (const item of value) appendListParam(searchParams, key, item);
    return;
  }
  searchParams.append(key, String(value));
}

function toQueryString(params: Record<string, unknown>): string {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    appendListParam(searchParams, key, value);
  }
  const query = searchParams.toString();
  return query ? `?${query}` : "";
}

export async function listBrainRuns(
  params: {
    cycle_key?: string;
    status?: BrainRunStatus[];
    trigger?: Array<"batch" | "manual" | "backfill">;
    include_superseded?: boolean;
    limit?: number;
  } = {},
  opts?: { signal?: AbortSignal }
): Promise<BrainRunsListResponse> {
  return fetchAPI<BrainRunsListResponse>(`/api/v1/brain/runs${toQueryString(params)}`, {
    signal: opts?.signal,
  });
}

// Subset of the digest-event payload the Diary actually needs to hydrate
// `what_changed` references (gh #27). The full backend model carries more,
// kept lean here so we don't widen the type surface for one render path.
export interface BrainDigestEventResponse {
  event_id: string;
  run_id: string;
  cycle_key: string;
  source_project?: string | null;
  target_project?: string | null;
  source_ref: string;
  title: string;
  summary?: string;
}

export interface BrainEventsListResponse {
  items: BrainDigestEventResponse[];
  next_cursor?: string | null;
  cycle_key?: string | null;
  run_id?: string | null;
  redacted_count?: number;
  total_returned?: number;
}

export async function listBrainEvents(
  params: {
    cycle_key?: string;
    run_id?: string;
    source_project?: string;
    cursor?: string;
    limit?: number;
  } = {},
  opts?: { signal?: AbortSignal }
): Promise<BrainEventsListResponse> {
  return fetchAPI<BrainEventsListResponse>(`/api/v1/brain/events${toQueryString(params)}`, {
    signal: opts?.signal,
  });
}

export async function listBrainJournal(
  params: {
    cycle_key?: string;
    run_id?: string;
    scope_type?: "company" | "program" | "project";
    scope_key?: string;
    program_key?: string;
    limit?: number;
  } = {},
  opts?: { signal?: AbortSignal }
): Promise<BrainJournalListResponse> {
  return fetchAPI<BrainJournalListResponse>(`/api/v1/brain/journal${toQueryString(params)}`, {
    signal: opts?.signal,
  });
}

export async function triggerBrainRun(
  opts?: { signal?: AbortSignal }
): Promise<BrainRunTriggerResponse> {
  return fetchAPI<BrainRunTriggerResponse>("/api/v1/brain/run", {
    method: "POST",
    signal: opts?.signal,
  });
}

export type OnboardingSetupSection = "Identità" | "Sorgenti" | "Ritmo" | "Fonti del brain";

export interface OnboardingScanCandidate {
  path: string;
  name: string;
  kind: "code" | "no-code";
}

export interface OnboardingScanResponse {
  root: string;
  exclusions: string[];
  proposals: OnboardingScanCandidate[];
}

export interface OnboardingSetupResponse {
  path: string;
  content: string;
  sections: Record<string, string>;
  checkboxes: Record<string, boolean>;
}

export interface OnboardingDemoSeedResponse {
  project: string;
  created: boolean;
  tasks: string[];
  todos: string[];
  lang: "it" | "en";
}

export interface OnboardingDemoTeardownResponse {
  project: string;
  tasks_deleted: number;
  todos_deleted: number;
  project_deleted: boolean;
}

export async function scanOnboardingWorkdir(
  data: { root: string; exclusions?: string[] },
  opts?: { signal?: AbortSignal },
): Promise<OnboardingScanResponse> {
  return fetchAPI<OnboardingScanResponse>("/api/v1/onboarding/scan", {
    method: "POST",
    body: JSON.stringify({ root: data.root, exclusions: data.exclusions ?? [] }),
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function readOnboardingSetup(
  opts?: { signal?: AbortSignal },
): Promise<OnboardingSetupResponse> {
  return fetchAPI<OnboardingSetupResponse>("/api/v1/onboarding/setup", {
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function writeOnboardingSetup(
  data: {
    section: OnboardingSetupSection;
    content?: string | null;
    checkboxes?: Record<string, boolean> | null;
  },
  opts?: { signal?: AbortSignal },
): Promise<OnboardingSetupResponse> {
  return fetchAPI<OnboardingSetupResponse>("/api/v1/onboarding/setup", {
    method: "PUT",
    body: JSON.stringify(data),
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function seedOnboardingDemo(
  lang: "it" | "en",
  opts?: { signal?: AbortSignal },
): Promise<OnboardingDemoSeedResponse> {
  return fetchAPI<OnboardingDemoSeedResponse>(`/api/v1/onboarding/demo?lang=${lang}`, {
    method: "POST",
    body: JSON.stringify({ lang }),
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function deleteOnboardingDemo(
  opts?: { signal?: AbortSignal },
): Promise<OnboardingDemoTeardownResponse> {
  return fetchAPI<OnboardingDemoTeardownResponse>("/api/v1/onboarding/demo", {
    method: "DELETE",
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function listTodosLocal(
  params: TodoListParamsLocal = {},
  opts?: { signal?: AbortSignal }
): Promise<TodoResponseLocal[]> {
  return fetchAPI<TodoResponseLocal[]>(`/api/v1/todos${toQueryString(params)}`, {
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function createTodoLocal(
  data: TodoCreateRequestLocal,
  opts?: { signal?: AbortSignal }
): Promise<TodoResponseLocal> {
  return fetchAPI<TodoResponseLocal>("/api/v1/todos", {
    method: "POST",
    body: JSON.stringify(data),
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function updateTodoLocal(
  todoId: string,
  data: TodoUpdateRequestLocal,
  opts?: { signal?: AbortSignal }
): Promise<TodoResponseLocal> {
  return fetchAPI<TodoResponseLocal>(
    `/api/v1/todos/${encodeURIComponent(todoId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(data),
      signal: opts?.signal,
      redirectOnUnauthorized: false,
    }
  );
}

export async function delegateTodoLocal(
  todoId: string,
  data: TodoDelegateRequestLocal = {},
  opts?: { signal?: AbortSignal }
): Promise<TodoResponseLocal> {
  return fetchAPI<TodoResponseLocal>(
    `/api/v1/todos/${encodeURIComponent(todoId)}/delegate`,
    {
      method: "POST",
      body: JSON.stringify(data),
      signal: opts?.signal,
      redirectOnUnauthorized: false,
    }
  );
}

// --- LLM / BYOK config (gh #22) ---
// The classifier resolves the BYOK key per call, so changes apply at runtime
// (no restart). Provider keys are write-only: the API never returns plaintext.

export type LlmFunctionName = "classify" | "embedding" | "brain";
export type LlmConfigStatus = "configured" | "disabled_no_provider";
export type LlmProvider =
  | "openai"
  | "anthropic"
  | "ollama"
  | "openai_compatible"
  | "mac_gateway";

export interface LlmStatusResponse {
  classify: LlmConfigStatus;
  /** False when BYOK_FERNET_SECRET is not set: provider-key creation is
   *  fail-closed (503). The Console surfaces this proactively (gh #22). */
  encryption_configured?: boolean;
}

export interface LlmConfigItem {
  function_name: LlmFunctionName;
  provider_key_id: string | null;
  provider: string | null;
  model: string | null;
  enabled: boolean;
  status: LlmConfigStatus;
}

export interface LlmConfigUpdateRequest {
  provider_key_id: string | null;
  model: string | null;
  enabled: boolean;
}

export interface ProviderKey {
  id: string;
  provider: string;
  label: string | null;
  base_url: string | null;
  has_key: boolean;
  key_prefix: string | null;
  key_status: "none" | "set" | "unreadable";
  created_at: string;
  updated_at: string;
}

export interface ProviderKeyCreateRequest {
  provider: LlmProvider;
  label?: string;
  api_key?: string;
  base_url?: string;
}

export async function getLlmStatus(
  opts?: { signal?: AbortSignal }
): Promise<LlmStatusResponse> {
  return fetchAPI<LlmStatusResponse>("/api/v1/llm-config/status", {
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function getLlmConfig(
  opts?: { signal?: AbortSignal }
): Promise<LlmConfigItem[]> {
  return fetchAPI<LlmConfigItem[]>("/api/v1/llm-config", {
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function putLlmConfig(
  functionName: LlmFunctionName,
  data: LlmConfigUpdateRequest,
  opts?: { signal?: AbortSignal }
): Promise<LlmConfigItem> {
  return fetchAPI<LlmConfigItem>(
    `/api/v1/llm-config/${encodeURIComponent(functionName)}`,
    {
      method: "PUT",
      body: JSON.stringify(data),
      signal: opts?.signal,
      redirectOnUnauthorized: false,
    }
  );
}

export async function listProviderKeys(
  opts?: { signal?: AbortSignal }
): Promise<ProviderKey[]> {
  return fetchAPI<ProviderKey[]>("/api/v1/llm-config/provider-keys", {
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function createProviderKey(
  data: ProviderKeyCreateRequest,
  opts?: { signal?: AbortSignal }
): Promise<ProviderKey> {
  return fetchAPI<ProviderKey>("/api/v1/llm-config/provider-keys", {
    method: "POST",
    body: JSON.stringify(data),
    signal: opts?.signal,
    redirectOnUnauthorized: false,
  });
}

export async function deleteProviderKey(
  keyId: string,
  opts?: { signal?: AbortSignal }
): Promise<void> {
  await fetchAPI<void>(
    `/api/v1/llm-config/provider-keys/${encodeURIComponent(keyId)}`,
    {
      method: "DELETE",
      signal: opts?.signal,
      redirectOnUnauthorized: false,
    }
  );
}

function virtualTodoOriginId(todo: TodoResponseLocal): string {
  return todo.origin?.id ?? todo.source_ref ?? todo.id.split(":").at(-1) ?? todo.id;
}

export async function applyVirtualTodoActionLocal(
  todo: TodoResponseLocal,
  action: VirtualTodoActionLocal,
  opts?: { feedback?: string; signal?: AbortSignal }
): Promise<unknown> {
  if (!todo.origin) {
    throw new APIError("Todo is not virtual", 422);
  }

  const originId = virtualTodoOriginId(todo);
  const reason = opts?.feedback || undefined;

  if (todo.origin.kind === "task_review") {
    if (action === "approve") {
      return fetchAPI(
        `/api/v1/pull_requests/${encodeURIComponent(originId)}/merge`,
        { method: "POST", signal: opts?.signal, redirectOnUnauthorized: false }
      );
    }
    return fetchAPI(
      `/api/v1/tasks/${encodeURIComponent(originId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          status: "in_progress",
          review_feedback: reason || "Returned from Todos review",
        }),
        signal: opts?.signal,
        redirectOnUnauthorized: false,
      }
    );
  }

  if (todo.origin.kind === "finding") {
    return fetchAPI(
      `/api/v1/brain/findings/${encodeURIComponent(originId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          approval_state: action === "approve" ? "approved" : "dismissed",
          reason: reason ?? null,
        }),
        signal: opts?.signal,
        redirectOnUnauthorized: false,
      }
    );
  }

  return fetchAPI(
    `/api/v1/brain/memory-operations/${encodeURIComponent(originId)}`,
    {
      method: "PATCH",
      body: JSON.stringify({
        approval_state: action === "approve" ? "approved" : "rejected",
        reason: reason ?? null,
      }),
      signal: opts?.signal,
      redirectOnUnauthorized: false,
    }
  );
}

// --- Settings ---

export async function getProjectDirs(): Promise<{ dirs: string[] }> {
  return fetchAPI<{ dirs: string[] }>("/api/v1/settings/project-dirs");
}

export async function updateProjectDirs(dirs: string[]): Promise<{ dirs: string[] }> {
  return fetchAPI<{ dirs: string[] }>("/api/v1/settings/project-dirs", {
    method: "PUT",
    body: JSON.stringify({ dirs }),
  });
}

// --- Session Intelligence ---

export async function getSessionMetrics(name: string): Promise<import("./types").SessionMetrics> {
  return fetchAPI<import("./types").SessionMetrics>(
    `/api/v1/sessions/${encodeURIComponent(name)}/metrics`
  );
}

export async function hibernateSession(name: string): Promise<void> {
  await fetchAPI(`/api/v1/sessions/${encodeURIComponent(name)}/hibernate`, {
    method: "POST",
  });
}

export async function resumeSession(name: string): Promise<void> {
  await fetchAPI(`/api/v1/sessions/${encodeURIComponent(name)}/resume`, {
    method: "POST",
  });
}

export async function restartSession(name: string): Promise<void> {
  await fetchAPI(`/api/v1/sessions/${encodeURIComponent(name)}/restart`, {
    method: "POST",
  });
}

// --- Files ---

export async function getProjectFile(
  slug: string,
  filePath: string,
  opts?: { signal?: AbortSignal }
): Promise<import("./types").FileContent> {
  return fetchAPI<import("./types").FileContent>(
    `/api/v1/projects/${encodeURIComponent(slug)}/files/${filePath}`,
    { signal: opts?.signal }
  );
}

export async function updateProjectFile(
  slug: string,
  filePath: string,
  content: string
): Promise<import("./types").FileContent> {
  return fetchAPI<import("./types").FileContent>(
    `/api/v1/projects/${encodeURIComponent(slug)}/files/${filePath}`,
    {
      method: "PUT",
      body: JSON.stringify({ content }),
    }
  );
}

// --- Costs (Sprint 4) ---

export async function getCostsSummary(
  params?: { from?: string; to?: string },
  opts?: { signal?: AbortSignal }
): Promise<ProjectCostSummary[]> {
  const searchParams = new URLSearchParams();
  if (params?.from) searchParams.set("from", params.from);
  if (params?.to) searchParams.set("to", params.to);
  const qs = searchParams.toString();
  return fetchAPI<ProjectCostSummary[]>(withQuery("/api/v1/costs/summary", qs), {
    signal: opts?.signal,
  });
}

export async function getProjectCosts(
  slug: string,
  params?: { from?: string; to?: string; limit?: number; offset?: number },
  opts?: { signal?: AbortSignal }
): Promise<ConversationCost[]> {
  const searchParams = new URLSearchParams();
  if (params?.from) searchParams.set("from", params.from);
  if (params?.to) searchParams.set("to", params.to);
  if (params?.limit) searchParams.set("limit", String(params.limit));
  if (params?.offset) searchParams.set("offset", String(params.offset));
  const qs = searchParams.toString();
  return fetchAPI<ConversationCost[]>(
    withQuery(`/api/v1/costs/by-project/${encodeURIComponent(slug)}`, qs),
    { signal: opts?.signal }
  );
}

export async function getProjectBilling(
  slug: string,
  params?: { from?: string; to?: string },
  opts?: { signal?: AbortSignal }
): Promise<ProjectBillingSummary> {
  const searchParams = new URLSearchParams();
  if (params?.from) searchParams.set("from", params.from);
  if (params?.to) searchParams.set("to", params.to);
  const qs = searchParams.toString();
  return fetchAPI<ProjectBillingSummary>(
    withQuery(`/api/v1/costs/billing/${encodeURIComponent(slug)}`, qs),
    { signal: opts?.signal }
  );
}

// --- Session UUID ---

export async function getSessionByUUID(
  uuid: string,
  opts?: { signal?: AbortSignal }
): Promise<Session> {
  return fetchAPI<Session>(
    `/api/v1/sessions/by-uuid/${encodeURIComponent(uuid)}`,
    { signal: opts?.signal }
  );
}

// --- Tags ---

export async function getTags(): Promise<import("./types").TagDefinition[]> {
  return fetchAPI<import("./types").TagDefinition[]>("/api/v1/tags");
}

// --- Finder ---

export async function getFinderTree(
  path = "",
  opts?: { signal?: AbortSignal }
): Promise<FinderTreeNode[]> {
  return fetchAPI<FinderTreeNode[]>(
    `/api/v1/finder/tree?path=${encodeURIComponent(path)}`,
    { signal: opts?.signal }
  );
}

export async function getFinderList(
  path = "",
  opts?: { signal?: AbortSignal }
): Promise<FinderListResponse> {
  return fetchAPI<FinderListResponse>(
    `/api/v1/finder/list?path=${encodeURIComponent(path)}`,
    { signal: opts?.signal }
  );
}

export async function getFinderFile(
  path: string,
  opts?: { signal?: AbortSignal }
): Promise<FinderFileContent> {
  return fetchAPI<FinderFileContent>(
    `/api/v1/finder/file?path=${encodeURIComponent(path)}`,
    { signal: opts?.signal }
  );
}

export async function saveFinderFile(
  path: string,
  content: string
): Promise<FinderFileContent> {
  return fetchAPI<FinderFileContent>(
    `/api/v1/finder/file?path=${encodeURIComponent(path)}`,
    {
      method: "PUT",
      body: JSON.stringify({ content }),
    }
  );
}

export async function finderMkdir(path: string): Promise<{ ok: boolean }> {
  return fetchAPI("/api/v1/finder/mkdir", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}

export async function finderRename(
  path: string,
  newName: string
): Promise<{ ok: boolean }> {
  return fetchAPI("/api/v1/finder/rename", {
    method: "POST",
    body: JSON.stringify({ path, new_name: newName }),
  });
}

export async function finderUpload(
  path: string,
  files: File[]
): Promise<FinderListItem[]> {
  const form = new FormData();
  form.append("path", path);
  files.forEach((f) => form.append("files", f));

  const res = await fetch(
    `${API_BASE_URL}/api/v1/finder/upload?path=${encodeURIComponent(path)}`,
    {
      method: "POST",
      credentials: "include",
      body: form,
    }
  );
  if (res.status === 401) {
    if (typeof window !== "undefined") window.location.href = "/login/";
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function finderDownload(path: string): Promise<Blob> {
  const res = await fetch(
    `${API_BASE_URL}/api/v1/finder/download?path=${encodeURIComponent(path)}`,
    { credentials: "include" }
  );
  if (!res.ok) throw new Error(`Download failed: ${res.status}`);
  return res.blob();
}

export async function finderDelete(path: string): Promise<{ ok: boolean }> {
  return fetchAPI("/api/v1/finder/delete", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
}

export async function finderMove(
  src: string,
  dest: string
): Promise<{ ok: boolean; new_path: string }> {
  return fetchAPI("/api/v1/finder/move", {
    method: "POST",
    body: JSON.stringify({ src, dest }),
  });
}

export async function getFinderPins(): Promise<Array<{id: number; path: string; label: string | null; position: number}>> {
  return fetchAPI("/api/v1/finder/pins");
}

export async function addFinderPin(path: string, label?: string): Promise<{id: number; path: string; label: string | null; position: number}> {
  return fetchAPI("/api/v1/finder/pins", {
    method: "POST",
    body: JSON.stringify({ path, label }),
  });
}

export async function removeFinderPin(pinId: number): Promise<void> {
  return fetchAPI(`/api/v1/finder/pins/${pinId}`, { method: "DELETE" });
}

// --- Pull Requests ---

export async function getMergeConflicts(
  project: string,
  opts?: { signal?: AbortSignal }
): Promise<MergeConflictResponse> {
  return fetchAPI<MergeConflictResponse>(
    `/api/v1/pull_requests/merge-conflicts?project=${encodeURIComponent(project)}`,
    { signal: opts?.signal }
  );
}

export async function getPullRequest(
  taskId: string,
  opts?: { signal?: AbortSignal }
): Promise<PullRequest> {
  return fetchAPI<PullRequest>(
    `/api/v1/pull_requests/${encodeURIComponent(taskId)}`,
    { signal: opts?.signal }
  );
}

export async function mergePullRequest(
  taskId: string
): Promise<{ merged: boolean; commit_sha: string; already_merged: boolean }> {
  return fetchAPI(`/api/v1/pull_requests/${encodeURIComponent(taskId)}/merge`, {
    method: "POST",
  });
}

export async function closePullRequest(
  taskId: string,
  reason?: string
): Promise<{ id: string; status: string; closed_reason: string }> {
  return fetchAPI(
    `/api/v1/pull_requests/${encodeURIComponent(taskId)}/close`,
    {
      method: "POST",
      body: JSON.stringify({ reason: reason || "" }),
    }
  );
}

export async function approvePR(taskId: string): Promise<PullRequest> {
  return fetchAPI<PullRequest>(
    `/api/v1/pull_requests/${encodeURIComponent(taskId)}/approve`,
    { method: "POST" }
  );
}

export async function requestPRChanges(taskId: string, comment: string): Promise<PullRequest> {
  return fetchAPI<PullRequest>(
    `/api/v1/pull_requests/${encodeURIComponent(taskId)}/request-changes`,
    {
      method: "POST",
      body: JSON.stringify({ comment }),
    }
  );
}

export async function revertPullRequest(taskId: string): Promise<{ revert_task_id: string; revert_pr_id: string; branch: string }> {
  const res = await fetch(`/api/v1/pull_requests/${taskId}/revert`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(typeof err.detail === "string" ? err.detail : `Revert failed: ${res.status}`);
  }
  return res.json();
}

// --- Upload ---

export interface UploadResult {
  path: string;
  filename: string;
  size: number;
  project: string;
}

export async function uploadFile(
  file: Blob,
  filename: string,
  options?: { session?: string }
): Promise<UploadResult> {
  const formData = new FormData();
  formData.append("file", file, filename);
  const params = options?.session ? `?session=${encodeURIComponent(options.session)}` : "";
  // Use XMLHttpRequest to bypass Next.js fetch patching which can cause
  // "Cannot read properties of undefined (reading 'payload')" errors
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE_URL}/terminal/upload${params}`);
    xhr.withCredentials = true; // send cookies cross-origin
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          reject(new Error("Invalid response"));
        }
      } else {
        try {
          const body = JSON.parse(xhr.responseText);
          reject(new Error(body.detail || `Upload failed: HTTP ${xhr.status}`));
        } catch {
          reject(new Error(`Upload failed: HTTP ${xhr.status}`));
        }
      }
    };
    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.send(formData);
  });
}

/** @deprecated Use uploadFile instead */
export async function uploadImage(file: Blob, filename: string): Promise<string> {
  const result = await uploadFile(file, filename);
  return result.path;
}

// --- Users ---

export async function listUsers(params?: { project?: string }): Promise<User[]> {
  const searchParams = new URLSearchParams();
  if (params?.project) searchParams.set("project", params.project);
  const qs = searchParams.toString();
  return fetchAPI<User[]>(withQuery("/api/v1/users", qs));
}

export async function createUser(data: UserCreateRequest): Promise<User> {
  return fetchAPI<User>("/api/v1/users", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateUser(id: string, data: Partial<UserCreateRequest>): Promise<User> {
  return fetchAPI<User>(`/api/v1/users/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteUser(id: string): Promise<void> {
  await fetchAPI<void>(`/api/v1/users/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function getUserRaci(userId: string): Promise<{ project: string; role: RaciRole }[]> {
  return fetchAPI<{ project: string; role: RaciRole }[]>(
    `/api/v1/users/${encodeURIComponent(userId)}/raci`
  );
}

// --- RACI ---

export async function getProjectRaci(slug: string): Promise<RaciEntry[]> {
  return fetchAPI<RaciEntry[]>(`/api/v1/projects/${encodeURIComponent(slug)}/raci`);
}

export async function addRaciEntry(
  slug: string,
  userId: string,
  role: RaciRole,
  reason?: string
): Promise<RaciEntry[]> {
  return fetchAPI<RaciEntry[]>(`/api/v1/projects/${encodeURIComponent(slug)}/raci`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId, role, reason }),
  });
}

export async function replaceRaci(
  slug: string,
  entries: { user_id: string; role: RaciRole; reason?: string }[]
): Promise<RaciEntry[]> {
  return fetchAPI<RaciEntry[]>(`/api/v1/projects/${encodeURIComponent(slug)}/raci`, {
    method: "PUT",
    body: JSON.stringify({ entries }),
  });
}

export async function removeRaciEntry(
  slug: string,
  userId: string,
  role: RaciRole
): Promise<void> {
  await fetchAPI<void>(
    `/api/v1/projects/${encodeURIComponent(slug)}/raci/${encodeURIComponent(userId)}/${role}`,
    { method: "DELETE" }
  );
}

// --- Teams ---

export async function getTeams(opts?: { signal?: AbortSignal }): Promise<Team[]> {
  return fetchAPI<Team[]>("/api/v1/teams", { signal: opts?.signal });
}

export async function createTeam(
  data: { display_name: string; slug?: string; description?: string; avatar_color?: string }
): Promise<Team> {
  return fetchAPI<Team>("/api/v1/teams", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateTeam(
  teamId: string,
  data: { display_name?: string; description?: string; avatar_color?: string }
): Promise<Team> {
  return fetchAPI<Team>(`/api/v1/teams/${encodeURIComponent(teamId)}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteTeam(teamId: string): Promise<void> {
  await fetchAPI<void>(`/api/v1/teams/${encodeURIComponent(teamId)}`, {
    method: "DELETE",
  });
}

export async function getTeamMembers(teamId: string, opts?: { signal?: AbortSignal }): Promise<TeamMember[]> {
  return fetchAPI<TeamMember[]>(`/api/v1/teams/${encodeURIComponent(teamId)}/members`, { signal: opts?.signal });
}

export async function addTeamMember(
  teamId: string,
  data: { user_id: string; role?: import("./types").TeamRole }
): Promise<{ status: string; team_id: string; user_id: string; role: string }> {
  return fetchAPI(`/api/v1/teams/${encodeURIComponent(teamId)}/members`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function removeTeamMember(teamId: string, userId: string): Promise<void> {
  await fetchAPI<void>(
    `/api/v1/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`,
    { method: "DELETE" }
  );
}

export async function getTeamProjects(
  teamId: string,
  opts?: { signal?: AbortSignal }
): Promise<TeamProject[]> {
  return fetchAPI<TeamProject[]>(`/api/v1/teams/${encodeURIComponent(teamId)}/projects`, { signal: opts?.signal });
}

export async function assignTeamProject(
  teamId: string,
  data: { project: string; is_public?: boolean }
): Promise<{ status: string; project: string; team_id: string }> {
  return fetchAPI(`/api/v1/teams/${encodeURIComponent(teamId)}/projects`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function removeTeamProject(teamId: string, slug: string): Promise<void> {
  await fetchAPI<void>(
    `/api/v1/teams/${encodeURIComponent(teamId)}/projects/${encodeURIComponent(slug)}`,
    { method: "DELETE" }
  );
}

export async function issueResetToken(
  userId: string
): Promise<{ token: string; user_id: string; slug: string }> {
  return fetchAPI("/api/v1/auth/admin/issue-reset-token", {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

// --- Task Cost Entries ---

export async function getTaskCostEntries(taskId: string): Promise<TaskCostSummary> {
  return fetchAPI<TaskCostSummary>(`/api/v1/tasks/${encodeURIComponent(taskId)}/cost-entries`);
}

export async function createHumanCostEntry(
  taskId: string,
  data: { human_minutes: number; description?: string; is_billable?: boolean; idempotency_key?: string }
): Promise<TaskCostSummary> {
  return fetchAPI<TaskCostSummary>(`/api/v1/tasks/${encodeURIComponent(taskId)}/cost-entries`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// --- Notifications ---

export async function listNotifications(
  params?: { limit?: number; unread_only?: boolean },
  opts?: { signal?: AbortSignal }
): Promise<Notification[]> {
  const query = new URLSearchParams();
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.unread_only) query.set("unread_only", "true");
  const qs = query.toString();
  return fetchAPI<Notification[]>(withQuery("/api/v1/notifications", qs), {
    signal: opts?.signal,
  });
}

export async function getUnreadNotificationCount(
  opts?: { signal?: AbortSignal }
): Promise<{ count: number }> {
  return fetchAPI<{ count: number }>("/api/v1/notifications/unread-count", {
    signal: opts?.signal,
  });
}

export async function markNotificationRead(id: string): Promise<void> {
  await fetchAPI(`/api/v1/notifications/${encodeURIComponent(id)}/read`, {
    method: "PATCH",
  });
}

export async function markNotificationActed(id: string): Promise<void> {
  await fetchAPI(`/api/v1/notifications/${encodeURIComponent(id)}/acted`, {
    method: "PATCH",
  });
}

export async function markAllNotificationsRead(): Promise<void> {
  await fetchAPI("/api/v1/notifications/mark-all-read", { method: "POST" });
}

// --- Bulk task operations (Anti-zombie D) ---
// Response shape for POST /api/v1/tasks/bulk-reject. Kept non-exported because
// callers consume it via bulkRejectTasks's inferred return type.
interface BulkRejectResponse {
  rejected: string[];
  failed: Array<{ task_id: string; error: string }>;
  total: number;
}

export async function bulkRejectTasks(
  task_ids: string[],
  reason: string = "aging_zombie",
  opts?: { signal?: AbortSignal }
): Promise<BulkRejectResponse> {
  return fetchAPI<BulkRejectResponse>("/api/v1/tasks/bulk-reject", {
    method: "POST",
    body: JSON.stringify({ task_ids, reason }),
    signal: opts?.signal,
  });
}

// --- CI Checks ---

export async function getCIChecks(
  taskId: string,
  opts?: { signal?: AbortSignal }
): Promise<CICheck[]> {
  return fetchAPI<CICheck[]>(
    `/api/v1/ci-checks?task_id=${encodeURIComponent(taskId)}`,
    { signal: opts?.signal }
  );
}

export async function getCIChecksSummary(
  taskId: string,
  opts?: { signal?: AbortSignal }
): Promise<CIChecksSummary> {
  return fetchAPI<CIChecksSummary>(
    `/api/v1/ci-checks/summary?task_id=${encodeURIComponent(taskId)}`,
    { signal: opts?.signal }
  );
}

// --- SSO ---

export async function getSSOConfig(
  domain: string,
  opts?: { signal?: AbortSignal }
): Promise<SSOConfig> {
  return fetchAPI<SSOConfig>(
    `/api/v1/auth/sso/config?domain=${encodeURIComponent(domain)}`,
    { signal: opts?.signal }
  );
}

export async function getSSOLoginUrl(
  workspaceId?: string
): Promise<{ redirect_url: string }> {
  const qs = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : "";
  return fetchAPI<{ redirect_url: string }>(`/api/v1/auth/sso/login${qs}`);
}

export async function completeSSOCallback(
  code: string,
  state: string,
  opts?: { signal?: AbortSignal }
): Promise<void> {
  await fetchAPI("/api/v1/auth/sso/callback", {
    method: "POST",
    body: JSON.stringify({ code, state }),
    signal: opts?.signal,
  });
}

// --- Workspaces ---

export async function getWorkspaces(
  opts?: { signal?: AbortSignal }
): Promise<Workspace[]> {
  return fetchAPI<Workspace[]>("/api/v1/workspaces", { signal: opts?.signal });
}

export async function inviteWorkspaceUser(
  workspaceId: string,
  invite: WorkspaceInvite
): Promise<void> {
  await fetchAPI(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/invites`, {
    method: "POST",
    body: JSON.stringify(invite),
  });
}

export async function updateUserRole(
  userId: string,
  role: string
): Promise<void> {
  await fetchAPI(`/api/v1/users/${encodeURIComponent(userId)}/role`, {
    method: "PUT",
    body: JSON.stringify({ role }),
  });
}

export async function getWorkspaceSSOConfig(
  workspaceId: string,
  opts?: { signal?: AbortSignal }
): Promise<SSOWorkspaceConfig> {
  return fetchAPI<SSOWorkspaceConfig>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/sso-config`,
    { signal: opts?.signal }
  );
}

export async function updateWorkspaceSSOConfig(
  workspaceId: string,
  config: { enabled: boolean; email_domains: string[] }
): Promise<void> {
  await fetchAPI(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/sso-config`, {
    method: "PUT",
    body: JSON.stringify(config),
  });
}

// --- Audit Log ---

type AuditApiEntry = {
  id: string;
  timestamp: string;
  action: string;
  user: string;
  resource_type: string;
  resource_id: string;
  details: Record<string, unknown> | null;
};

function auditEventType(entry: AuditApiEntry): AuditLogResponse["entries"][number]["event_type"] {
  if (entry.action === "pr.merge") return "pr_merged";
  if (entry.action === "task.status_changed" && entry.details?.to_status === "completed") {
    return "task_completed";
  }
  if (entry.action === "task.completed") return "task_completed";
  if (entry.action === "auth.login") return "login";
  if (entry.action === "auth.logout") return "logout";
  if (entry.action === "user.invite") return "user_invited";
  if (entry.action === "sso.configure") return "sso_configured";
  return "audit_entry";
}

function describeAuditEntry(entry: AuditApiEntry): string {
  const target = [entry.resource_type, entry.resource_id].filter(Boolean).join(" ");
  return target ? `${entry.action} on ${target}` : entry.action;
}

function mapAuditEntry(entry: AuditApiEntry): AuditLogResponse["entries"][number] {
  return {
    id: entry.id,
    timestamp: entry.timestamp,
    user_id: entry.user,
    user_name: entry.user || "system",
    event_type: auditEventType(entry),
    description: describeAuditEntry(entry),
    metadata: entry.details,
  };
}

export async function getAuditLog(
  params?: {
    action?: string;
    user?: string;
    resource_type?: string;
    resource_id?: string;
    offset?: number;
    limit?: number;
  },
  opts?: { signal?: AbortSignal }
): Promise<AuditLogResponse> {
  const qs = new URLSearchParams();
  if (params?.action) qs.set("action", params.action);
  if (params?.user) qs.set("user", params.user);
  if (params?.resource_type) qs.set("resource_type", params.resource_type);
  if (params?.resource_id) qs.set("resource_id", params.resource_id);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const q = qs.toString();
  const rows = await fetchAPI<AuditApiEntry[]>(
    withQuery("/api/v1/audit", q),
    { signal: opts?.signal }
  );

  const offset = params?.offset ?? 0;
  const requestedLimit = params?.limit;
  return {
    entries: rows.map(mapAuditEntry),
    next_cursor: requestedLimit && rows.length === requestedLimit
      ? String(offset + rows.length)
      : null,
    total: offset + rows.length,
  };
}


// --- Search (Semantic) ---

export interface SearchHit {
  doc_type: "task" | "project" | "file" | "handoff";
  doc_id: string;
  title: string;
  project: string;
  score: number;
  path?: string;
  status?: string | null;
  // Phase 6.5 A hybrid-search extensions (all optional — legacy semantic path omits).
  edge_path?: string[] | null;
  edge_path_summary?: string | null;
  rrf_score?: number | null;
}

export type DocType = SearchHit["doc_type"];

export interface SearchResponse {
  tasks: SearchHit[];
  projects: SearchHit[];
  files: SearchHit[];
  handoffs: SearchHit[];
  total: number;
  query: string;
  suggested_next_tool?: string[] | null;
}

export async function globalSearch(
  q: string,
  opts?: { signal?: AbortSignal }
): Promise<SearchResponse> {
  const qs = new URLSearchParams({ q });
  return fetchAPI<SearchResponse>(`/api/v1/search?${qs.toString()}`, {
    signal: opts?.signal,
  });
}

// --- Monitoring ---

export async function getMonitoringCurrent(
  opts?: { signal?: AbortSignal }
): Promise<MonitoringSnapshot> {
  return fetchAPI<MonitoringSnapshot>("/api/v1/monitoring/current", {
    signal: opts?.signal,
  });
}

export async function getMonitoringHistory(
  metric: string,
  range: string,
  opts?: { signal?: AbortSignal }
): Promise<CandleDatapoint[]> {
  return fetchAPI<CandleDatapoint[]>(
    `/api/v1/monitoring/history?metric=${encodeURIComponent(metric)}&range=${encodeURIComponent(range)}`,
    { signal: opts?.signal }
  );
}

export async function getMonitoringSecurity(
  opts?: { signal?: AbortSignal }
): Promise<SecurityData> {
  return fetchAPI<SecurityData>("/api/v1/monitoring/security", {
    signal: opts?.signal,
  });
}

export async function getMonitoringDiskTree(
  opts?: { path?: string; signal?: AbortSignal }
): Promise<DiskTreeResponse> {
  const qs =
    opts?.path && opts.path !== "/"
      ? `?path=${encodeURIComponent(opts.path)}`
      : "";
  return fetchAPI<DiskTreeResponse>(`/api/v1/monitoring/disk-tree${qs}`, {
    signal: opts?.signal,
  });
}

// --- Newsletter ---

export async function getNewsletterCompose(
  opts?: { signal?: AbortSignal }
): Promise<{
  rubriche: import("./types").NewsletterRubrica[];
  stats: { total: number; by_rubrica: Record<string, number> };
  auto_selected_ids: string[];
}> {
  return fetchAPI("/api/v1/newsletter/compose", { signal: opts?.signal });
}

export async function postNewsletterPreview(
  itemIds: string[]
): Promise<{ html: string; subject: string }> {
  return fetchAPI("/api/v1/newsletter/preview", {
    method: "POST",
    body: JSON.stringify({ item_ids: itemIds }),
  });
}

export async function postNewsletterSend(
  itemIds: string[],
  subject?: string
): Promise<{ edition_id: string; recipients_count: number; items_sent: number }> {
  return fetchAPI("/api/v1/newsletter/send", {
    method: "POST",
    body: JSON.stringify({ item_ids: itemIds, subject }),
  });
}

export async function getNewsletterRecipients(
  opts?: { signal?: AbortSignal }
): Promise<import("./types").NewsletterRecipient[]> {
  return fetchAPI("/api/v1/newsletter/recipients", { signal: opts?.signal });
}

export async function addNewsletterRecipient(
  email: string,
  name?: string
): Promise<import("./types").NewsletterRecipient> {
  return fetchAPI("/api/v1/newsletter/recipients", {
    method: "POST",
    body: JSON.stringify({ email, name }),
  });
}

export async function removeNewsletterRecipient(id: string): Promise<void> {
  await fetchAPI(`/api/v1/newsletter/recipients/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

// --- Inbox Sources (PR D/5) ---

type InboxSourceRaw = Omit<import("./types").InboxSource, "active"> & {
  active: boolean | number;
};

function normalizeInboxSource(raw: InboxSourceRaw): import("./types").InboxSource {
  return {
    ...raw,
    active: Boolean(raw.active),
  };
}

export async function listInboxSources(
  activeOnly = false,
  opts?: { signal?: AbortSignal }
): Promise<import("./types").InboxSource[]> {
  const params = new URLSearchParams({ active_only: String(activeOnly) });
  const raw = await fetchAPI<InboxSourceRaw[]>(
    `/api/v1/inbox/sources?${params.toString()}`,
    { signal: opts?.signal }
  );
  return raw.map(normalizeInboxSource);
}

export async function getInboxSource(
  id: string,
  opts?: { signal?: AbortSignal }
): Promise<import("./types").InboxSource> {
  const raw = await fetchAPI<InboxSourceRaw>(
    `/api/v1/inbox/sources/${encodeURIComponent(id)}`,
    { signal: opts?.signal }
  );
  return normalizeInboxSource(raw);
}

export async function getInboxSourceMetrics(
  id: string,
  range: import("./types").SourceMetricsRange = "total",
  opts?: { signal?: AbortSignal }
): Promise<import("./types").InboxSourceMetrics> {
  const params = new URLSearchParams({ range });
  return fetchAPI<import("./types").InboxSourceMetrics>(
    `/api/v1/inbox/sources/${encodeURIComponent(id)}/metrics?${params.toString()}`,
    { signal: opts?.signal }
  );
}

export type CreateInboxSourceBody = {
  name: string;
  source_key: string;
  feed_url?: string | null;
  source_type?: import("./types").SourceType;
};

export async function createInboxSource(
  body: CreateInboxSourceBody
): Promise<import("./types").InboxSource> {
  const raw = await fetchAPI<InboxSourceRaw>("/api/v1/inbox/sources", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return normalizeInboxSource(raw);
}

export type UpdateInboxSourceBody = {
  name?: string;
  feed_url?: string | null;
  active?: boolean;
};

export async function updateInboxSource(
  id: string,
  body: UpdateInboxSourceBody
): Promise<import("./types").InboxSource> {
  const raw = await fetchAPI<InboxSourceRaw>(
    `/api/v1/inbox/sources/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    }
  );
  return normalizeInboxSource(raw);
}

export async function deleteInboxSource(id: string): Promise<void> {
  await fetchAPI<void>(
    `/api/v1/inbox/sources/${encodeURIComponent(id)}`,
    { method: "DELETE" }
  );
}

// --- Newsletter editions archive ---

export type NewsletterEditionSummary = {
  id: string;
  edition_number: number;
  subject: string;
  sent_at: string;
  sent_by: string;
  recipient_count: number;
  item_count: number;
};

export type NewsletterEditionDetail = NewsletterEditionSummary & {
  html_content: string;
  item_ids: string[];
};

export async function listNewsletterEditions(
  limit = 20,
  beforeSentAt?: string,
  opts?: { signal?: AbortSignal }
): Promise<NewsletterEditionSummary[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeSentAt) params.set("before_sent_at", beforeSentAt);
  return fetchAPI(`/api/v1/newsletter/editions?${params.toString()}`, {
    signal: opts?.signal,
  });
}

export async function getNewsletterEdition(
  editionId: string,
  opts?: { signal?: AbortSignal }
): Promise<NewsletterEditionDetail> {
  return fetchAPI(`/api/v1/newsletter/editions/${encodeURIComponent(editionId)}`, {
    signal: opts?.signal,
  });
}

// --- Graph Cosmo canvas adapter (PR #3) ---

import { GraphCosmoZ, type GraphCosmo } from "@/components/graph/cosmo/types";

/**
 * Fetch `/graph/cosmo` — bundle project super-nodi + aggregated edges per il
 * canvas Cosmo. Validazione Zod live contro schema strict (project/edge cap).
 */
export async function getGraphCosmo(opts?: { signal?: AbortSignal }): Promise<GraphCosmo> {
  return fetchAPIValidated("/api/v1/graph/cosmo", GraphCosmoZ, { signal: opts?.signal });
}

// --- KG PR-Impact sub-03 MVP fetchers --------------------------------------

import {
  BranchesResponseZ,
  ConflictsResponseZ,
  PrImpactResponseZ,
  type BranchesResponse,
  type ConflictsResponse,
  type PrImpactResponse,
} from "@/components/graph/pr-impact/types";

/**
 * Fetch the full impact bundle for a PR: metadata + modified functions
 * (paginated) + depth-1 transitive impact. The endpoint accepts either
 * the bare task UUID or the canonical `pr:artifact:<uuid>` form; we pass
 * through whatever the caller gives us.
 */
export async function getPrImpact(
  prId: string,
  opts: {
    depth?: number;
    offset?: number;
    limit?: number;
    include_all?: boolean;
    signal?: AbortSignal;
  } = {}
): Promise<PrImpactResponse> {
  const params = new URLSearchParams();
  if (opts.depth !== undefined) params.set("depth", String(opts.depth));
  if (opts.offset !== undefined) params.set("offset", String(opts.offset));
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.include_all !== undefined)
    params.set("include_all", String(opts.include_all));
  const qs = params.toString();
  const suffix = qs ? `?${qs}` : "";
  return fetchAPIValidated(
    `/api/v1/graph/pr-impact/${encodeURIComponent(prId)}${suffix}`,
    PrImpactResponseZ,
    { signal: opts.signal }
  );
}

/**
 * List branches with their open-PR rollup. `state="active"` returns only
 * branches that have a draft/open/merging PR — useful for the lens sidebar.
 */
export async function listPrImpactBranches(
  opts: {
    state?: "active" | "stale" | "all";
    project?: string;
    offset?: number;
    limit?: number;
    signal?: AbortSignal;
  } = {}
): Promise<BranchesResponse> {
  const params = new URLSearchParams();
  if (opts.state) params.set("state", opts.state);
  if (opts.project) params.set("project", opts.project);
  if (opts.offset !== undefined) params.set("offset", String(opts.offset));
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const suffix = qs ? `?${qs}` : "";
  return fetchAPIValidated(
    `/api/v1/graph/branches${suffix}`,
    BranchesResponseZ,
    { signal: opts.signal }
  );
}

/**
 * Detect shared touched functions across 2-5 PRs. The endpoint accepts
 * either bare task UUIDs or canonical `pr:artifact:<uuid>` forms — we
 * forward the input verbatim.
 */
export async function findPrImpactConflicts(
  prIds: string[],
  opts: { project?: string; signal?: AbortSignal } = {}
): Promise<ConflictsResponse> {
  const params = new URLSearchParams();
  if (opts.project) params.set("project", opts.project);
  for (const id of prIds) params.append("pr_ids", id);
  return fetchAPIValidated(
    `/api/v1/graph/conflicts?${params.toString()}`,
    ConflictsResponseZ,
    { signal: opts.signal }
  );
}

// --- Codex modules + functions (sub-03 zoom-levels) -----------------------

import {
  CodexFunctionsResponseZ,
  CodexModulesResponseZ,
  type CodexFunctionsResponse,
  type CodexModulesResponse,
} from "@/components/graph/pr-impact/types";

export async function getCodexModules(
  opts: { project?: string; limit?: number; signal?: AbortSignal } = {}
): Promise<CodexModulesResponse> {
  const params = new URLSearchParams();
  if (opts.project) params.set("project", opts.project);
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return fetchAPIValidated(
    withQuery("/api/v1/graph/codex-modules", qs),
    CodexModulesResponseZ,
    { signal: opts.signal }
  );
}

export async function getCodexFunctions(
  module: string,
  opts: { project?: string; limit?: number; signal?: AbortSignal } = {}
): Promise<CodexFunctionsResponse> {
  const params = new URLSearchParams();
  params.set("module", module);
  if (opts.project) params.set("project", opts.project);
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  return fetchAPIValidated(
    `/api/v1/graph/codex-functions?${params.toString()}`,
    CodexFunctionsResponseZ,
    { signal: opts.signal }
  );
}
