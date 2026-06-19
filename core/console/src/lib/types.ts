export type SessionProvider = "claude" | "gemini" | "codex" | "opencode";

export interface Session {
  name: string;
  display_name: string | null;
  pinned: boolean;
  sort_order: number;
  group_name: string | null;
  project_slug: string | null;
  session_uuid: string | null;
  status: string | null;
  provider: SessionProvider;
  created_at: string | null;
  last_active: string | null;
  attached: boolean;
  // Intelligence fields
  hibernated: boolean;
  conversation_id: string | null;
  model: string | null;
  launch_model?: string | null;
  permission_preset?: string | null;
  last_context_pct: number | null;
  last_cost_usd: number | null;
  last_message_count: number | null;
  // PR2 dual metrics (migration 087) — optional so v1 clients still compile
  last_context_pct_real?: number | null;
  last_context_pct_scaled?: number | null;
  last_cost_conversation_usd?: number | null;
  last_cost_session_usd?: number | null;
  last_cost_session_incomplete?: boolean;
  last_input_tokens?: number | null;
  last_output_tokens?: number | null;
  last_reasoning_tokens?: number | null;
  working_seconds_msg?: number | null;
  metrics_refreshed_at?: string | null;
  pricing_version?: string | null;
  // PR4 shadow cost (migration 089) — optional for v1/v2 client compat
  last_cost_conversation_equivalent_usd?: number | null;
  last_cost_session_equivalent_usd?: number | null;
  last_cost_equivalent_pricing_version?: string | null;
  conversation_ids?: string[];
  auto_hibernate_minutes: number;
  activity_state: "working" | "idle" | "needs_input" | "active" | null;
  // Process metrics
  cpu_pct: number | null;
  ram_mb: number | null;
  // Time tracking
  working_seconds: number;
  created_epoch: number | null;
  completed_at: string | null;
  // Agent management
  agent_managed: boolean;
  // Ownership (migration 033)
  owner_id?: string | null;
}

export interface SessionCatalogModel {
  id: string;
  label: string;
  description: string;
  context_window: number | null;
  supports_1m: boolean;
  recommended: boolean;
  experimental: boolean;
  note: string | null;
}

export interface SessionPermissionPreset {
  id: string;
  label: string;
  badge: string;
  description: string;
}

export interface SessionCatalogProvider {
  id: SessionProvider;
  label: string;
  default_model: string;
  launch_root: "project" | "workspace";
  models: SessionCatalogModel[];
  permission_presets: SessionPermissionPreset[];
  note: string | null;
}

export interface SessionCatalogResponse {
  providers: SessionCatalogProvider[];
}

export type SystemRole = "super_admin" | "admin" | "operator" | "viewer";

export type TeamRole = "member" | "admin";

export interface TeamSummary {
  id: string;
  slug: string;
  display_name: string;
  role: TeamRole;
}

export interface Team {
  id: string;
  slug: string;
  display_name: string;
  description?: string;
  avatar_color?: string;
  created_at: string;
  member_count?: number;
  project_count?: number;
}

export interface TeamMember {
  user_id: string;
  display_name: string;
  system_role: string;
  role: TeamRole;
  joined_at: string;
}

export interface TeamProject {
  project: string;
  is_public: boolean;
  assigned_at: string;
}

export interface AppCapabilities {
  /** Todos classifier wants the gateway LLM but no key is configured → advanced
   *  auto-classification falls back to the heuristic (gh #22). */
  todos_llm_key_missing?: boolean;
}

export interface UserInfo {
  username: string;
  user_id: string;
  system_role: SystemRole;
  display_name: string;
  teams: TeamSummary[];
  capabilities?: AppCapabilities;
}

export interface TicketResponse {
  ticket: string;
}

export interface TerminalMetricDistribution {
  count: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
  max: number | null;
}

export interface TerminalMetricsSessionSnapshot {
  pty_read_bytes_per_sec: number;
  pty_read_samples: number;
  pty_write_duration_ms: TerminalMetricDistribution;
  fanout_duration_ms: TerminalMetricDistribution;
  fanout_connection_count_max: number;
  websocket_ping_rtt_ms?: TerminalMetricDistribution;
  live_websocket_count: number;
}

export interface TerminalTicketMetricsSnapshot {
  issue_duration_ms: TerminalMetricDistribution;
  issue_lock_wait_ms: TerminalMetricDistribution;
  issue_insert_ms: TerminalMetricDistribution;
  issue_commit_ms: TerminalMetricDistribution;
  consume_duration_ms: TerminalMetricDistribution;
  consume_lock_wait_ms: TerminalMetricDistribution;
  consume_lookup_ms: TerminalMetricDistribution;
  consume_update_ms: TerminalMetricDistribution;
  consume_commit_ms: TerminalMetricDistribution;
  outcome_counts: Record<string, number>;
  last_issue_event: Record<string, unknown> | null;
  last_consume_event: Record<string, unknown> | null;
}

export interface WriterLockMetricsSnapshot {
  window_seconds: number;
  locked: boolean;
  current_holder: Record<string, unknown> | null;
  wait_ms: TerminalMetricDistribution;
  hold_ms: TerminalMetricDistribution;
  contended_wait_count: number;
  slow_wait_count: number;
  wait_by_label: Record<string, TerminalMetricDistribution>;
  hold_by_label: Record<string, TerminalMetricDistribution>;
  blocked_by_label_counts: Record<string, number>;
  last_wait_events: Array<Record<string, unknown>>;
  last_hold_events: Array<Record<string, unknown>>;
}

export interface TerminalMetricsSnapshot {
  timestamp: number;
  window_seconds: number;
  live_websocket_count: number;
  live_pty_reader_count: number;
  process?: {
    pid: number;
    rss_bytes: number;
    max_rss_bytes: number;
    user_cpu_seconds: number;
    system_cpu_seconds: number;
    open_fd_count: number | null;
    thread_count: number | null;
  };
  writer_lock?: WriterLockMetricsSnapshot;
  network?: {
    event_loop_lag_ms: TerminalMetricDistribution;
    websocket_ping_rtt_ms: TerminalMetricDistribution;
    internet_probe_duration_ms: TerminalMetricDistribution;
    internet_probes: Array<{
      target: string;
      ok: boolean;
      timestamp: number;
      duration_ms: number;
      status_code: number | null;
      bytes_received: number;
      bytes_per_sec: number | null;
      error: string | null;
    }>;
  };
  sessions_control?: {
    list_duration_ms: TerminalMetricDistribution;
    sync_duration_ms: TerminalMetricDistribution;
    cache_state_counts: Record<string, number>;
    last_list_event: Record<string, unknown> | null;
    last_sync_event: Record<string, unknown> | null;
  };
  terminal_ticket?: TerminalTicketMetricsSnapshot;
  client_event_ingest?: {
    batches: number;
    events: number;
    last_batch_at: number | null;
  };
  sessions: Record<string, TerminalMetricsSessionSnapshot>;
}

export interface TerminalNetworkProbeResponse {
  server_internet_probe: {
    target: string;
    url: string;
    ok: boolean;
    status_code: number | null;
    duration_ms: number;
    bytes_received: number;
    error: string | null;
  };
  client_host: string | null;
  payload_bytes: number;
  padding: string;
}

export type AuthStatus = "loading" | "authenticated" | "unauthenticated";

export type WSConnectionStatus =
  | "connecting"
  | "connected"
  | "disconnected"
  | "error";

export interface TerminalProps {
  sessionName: string;
  isActive: boolean;
}

// --- Projects Module (Sprint 3) ---

export type ProjectStatus = "active" | "paused" | "blocked" | "completed" | "not_started";
export type CommentStatus = "info" | "question" | "blocker" | "resolved";
export type TargetType = "program" | "project" | "task";
export type ReactionType = "+1" | "-1" | "eyes" | "check";

export interface StatusCounts {
  pending: number;
  approved: number;
  in_progress: number;
  review: number;
  completed: number;
  rejected: number;
  failed: number;
}

export type ProjectType = "work" | "code" | "system";
export type ProjectLifecycle = "idea" | "planning" | "active" | "maintenance" | "archived";
export type ProjectScope = "personal" | "work";

export interface ProjectInfo {
  slug: string;
  name: string;
  program: string | null;
  language: string | null;
  lifecycle: ProjectLifecycle | null;
  phase: string | null;
  scope: ProjectScope | null;
  description: string | null;
  type: ProjectType | null;
  repo_path: string | null;
  metadata_path: string | null;
  status: ProjectStatus | null;
  task_counts: StatusCounts;
  last_handoff: string | null;
  last_status_update: string | null;
  on_server: boolean;
  path?: string | null;
  color?: string | null;
}

export interface HandoffEntry {
  filename: string;
  date: string;
  summary: string;
  session: string | null;
  branch: string | null;
  tags: string[];
}

export interface DocEntry {
  filename: string;
  date: string | null;
  title: string | null;
  category: string | null;
}

export interface DeployInfo {
  hosting: string | null;
  url: string | null;
  api_url: string | null;
  git_connected: boolean;
}

export interface ProjectDetail {
  slug: string;
  name: string;
  program: string | null;
  language: string | null;
  lifecycle: ProjectLifecycle | null;
  phase: string | null;
  scope: ProjectScope | null;
  description: string | null;
  type: ProjectType | null;
  repo_path: string | null;
  metadata_path: string | null;
  context_md: string | null;
  config: Record<string, string>;
  deploy: DeployInfo | null;
  color?: string | null;
  handoffs: HandoffEntry[];
  plans: DocEntry[];
  solutions: DocEntry[];
  kg_context?: ProjectKgContext | null;
}

export interface ProgramInfo {
  name: string;
  description: string;
  projects: ProjectInfo[];
}

export interface ProjectKgNeighbor {
  id: string;
  type: string;
  name: string;
  project_id?: string | null;
  relation: string;
  edge?: {
    relation?: string | null;
    direction?: string | null;
  } | null;
}

export interface ProjectKgContext {
  neighbors?: ProjectKgNeighbor[];
}

export interface LearningResponse {
  id: string;
  title: string;
  category: string;
  description: string;
  tags: string[];
  module: string | null;
  severity: string;
  frequency: number;
  last_occurrence: string | null;
  prevention: string | null;
  session: number | null;
  project: string | null;
  created_at: string;
  updated_at: string | null;
  kg_context?: Record<string, unknown> | null;
}

export type ManualProjectEdgeKind = "related" | "depends_on";

export interface ManualProjectEdge {
  src_slug: string;
  dst_slug: string;
  kind: ManualProjectEdgeKind;
  provenance: "manual";
}

export interface ManualProjectEdgeWriteResponse {
  created?: boolean;
  deleted?: boolean;
  edge: ManualProjectEdge;
}

export interface GitCommit {
  hash: string;
  hash_short: string;
  message: string;
  author: string;
  date: string;
}

export interface GitBranch {
  name: string;
  is_current: boolean;
}

// --- Git Graph Visualization ---

export interface GitGraphCommit {
  hash: string;
  hash_short: string;
  parents: string[];
  refs: string[];
  message: string;
  author: string;
  date: string;
}

export interface GitRef {
  name: string;
  hash_short: string;
  type: "commit" | "tag";
}

export interface GitGraphResponse {
  commits: GitGraphCommit[];
  refs: GitRef[];
  has_more: boolean;
}

export interface GitCommitDetail {
  hash: string;
  body: string;
  author: string;
  email: string;
  date: string;
  stats: string[];
}

export interface GraphNode {
  commit: GitGraphCommit;
  row: number;
  lane: number;
  color: string;
}

export interface GraphEdge {
  fromHash: string;
  toHash: string;
  fromLane: number;
  toLane: number;
  fromRow: number;
  toRow: number;
  color: string;
  type: "branch" | "merge";
}

export interface StatusUpdateCreate {
  project: string;
  status: ProjectStatus;
  what_done: string | null;
  blockers: string | null;
  next_steps: string | null;
}

export interface StatusUpdateResponse {
  id: number;
  project: string;
  status: ProjectStatus;
  what_done: string | null;
  blockers: string | null;
  next_steps: string | null;
  created_by: string;
  created_at: string;
  updated_at: string | null;
}

// --- Feed-style status updates (PR #9 single-pager v2) ---

export type StatusUpdateKind = "manual" | "auto_handoff" | "auto_commit" | "ai_summary";

export interface StatusUpdateFeedItem {
  id: string;
  kind: StatusUpdateKind;
  author: string;
  author_display: string | null;
  content_md: string;
  ref_id: string | null;
  created_at: string;
  derived: boolean;
}

export interface StatusUpdateFeedResponse {
  updates: StatusUpdateFeedItem[];
  total: number;
}

export interface CommentCreate {
  target_type: TargetType;
  target_id: string;
  body: string;
  status: CommentStatus;
  parent_id: number | null;
}

export interface CommentUpdate {
  body?: string;
  status?: CommentStatus;
}

export interface CommentReaction {
  reaction: ReactionType;
  created_by: string;
}

export interface CommentResponse {
  id: number;
  target_type: TargetType;
  target_id: string;
  body: string;
  status: CommentStatus;
  created_by: string;
  created_at: string;
  edited_at: string | null;
  parent_id: number | null;
  reactions: CommentReaction[];
  replies: CommentResponse[];
}

// --- Task Triage Module ---

export type TaskStatus = "pending" | "approved" | "in_progress" | "review" | "completed" | "rejected" | "failed";
export type TaskPriority = "low" | "medium" | "high";
export type TaskKind = "normal" | "idea";
export type TaskSource = "telegram" | "manual" | "session" | "console" | "rem_proposal";
export type DelegationType = "agent" | "hybrid" | "human";
export type TaskCompletionMode = "pr" | "doc" | "none";

export interface TaskResponse {
  id: string;
  title: string;
  description: string | null;
  kind: TaskKind;
  status: TaskStatus;
  project: string;
  priority: TaskPriority;
  created_by: string;
  owner_id: string | null;
  owner: UserSummary | null;
  source: TaskSource;
  source_ref: string | null;
  tags: string[];
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
  impact: number | null;
  confidence: number | null;
  ease: number | null;
  delegation: DelegationType | null;
  ice_score: number | null;
  scored_by: string | null;
  scored_at: string | null;
  pr_status: PrStatus | null;
  review_feedback: string | null;
  due_date?: string | null;
  reminder_sent_at?: string | null;
  completion_mode?: TaskCompletionMode;
  comments?: CommentResponse[] | null;
  kg_context?: Record<string, unknown> | null;
  blocked_by?: string | null;
  blocks?: string[] | null;
}

/** TaskResponse enriched with program name (resolved client-side from project slug) */
export interface TriageTask extends TaskResponse {
  program: string | null;
}

export interface TriageFilters {
  status: TaskStatus[];
  kind: TaskKind[];
  project: string[];
  priority: TaskPriority[];
  delegation: (DelegationType | "unscored")[];
}

export interface TaskCreateRequest {
  title: string;
  description?: string | null;
  project: string;
  kind?: TaskKind;
  priority?: TaskPriority;
  source: TaskSource;
  source_ref?: string | null;
  owner_id?: string | null;
  tags?: string[];
  impact?: number | null;
  confidence?: number | null;
  ease?: number | null;
  delegation?: DelegationType | null;
  due_date?: string | null;
  completion_mode?: TaskCompletionMode;
}

export interface TaskUpdateRequest {
  title?: string;
  description?: string | null;
  status?: TaskStatus;
  kind?: TaskKind;
  priority?: TaskPriority;
  owner_id?: string | null;
  review_feedback?: string | null;
  tags?: string[];
  impact?: number | null;
  confidence?: number | null;
  ease?: number | null;
  delegation?: DelegationType | null;
  due_date?: string | null;
  completion_mode?: TaskCompletionMode;
}

// --- Users & RACI ---

export interface UserSummary {
  id: string;
  slug: string;
  display_name: string;
  avatar_color: string;
}

export interface User {
  id: string;
  slug: string;
  display_name: string;
  type: "human" | "agent";
  email: string | null;
  avatar_color: string;
  system_role: string;
  notification_channels: string[];
  telegram_chat_id: string | null;
  last_used_at: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
  teams: TeamSummary[];
  // Fase 3 — Linux provisioning
  linux_username: string | null;
  provisioned_at: string | null;
  onboarding_completed: boolean;
}

export type RaciRole = "responsible" | "accountable" | "consulted" | "informed";

export interface RaciEntry {
  user: UserSummary;
  role: RaciRole;
}

export interface UserCreateRequest {
  slug: string;
  display_name: string;
  type: "human" | "agent";
  email?: string | null;
  avatar_color?: string | null;
  system_role?: string;
  notification_channels?: string[];
  telegram_chat_id?: string | null;
}

export interface FileContent {
  content: string;
  filename: string;
  path: string;
  size: number;
}

export type SessionActivityStatus = "working" | "waiting" | "idle";

export interface SessionMetrics {
  conversation_id: string | null;
  model: string | null;
  context_pct: number;
  cost_usd: number;
  message_count: number;
  duration_minutes: number;
  hibernated: boolean;
  auto_hibernate_minutes: number;
  // PR2 dual metrics
  context_pct_real?: number | null;
  context_pct_scaled?: number | null;
  cost_conversation_usd?: number | null;
  cost_session_usd?: number | null;
  cost_session_incomplete?: boolean;
  input_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number | null;
  working_seconds_msg?: number | null;
  pricing_version?: string | null;
  conversation_ids?: string[];
  // PR4 shadow cost
  cost_conversation_equivalent_usd?: number | null;
  cost_session_equivalent_usd?: number | null;
  cost_equivalent_pricing_version?: string | null;
}

// --- Cost Tracking (Sprint 4) ---

export interface ProjectCostSummary {
  project_slug: string;
  program: string | null;
  total_cost_usd: number;
  conversation_count: number;
}

export interface ConversationCost {
  conversation_id: string;
  session_name: string | null;
  display_name: string | null;
  model: string | null;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  message_count: number;
  working_seconds: number;
  created_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
}

export interface TagDefinition {
  id: string;
  label: string;
  category: "layer" | "type" | "domain";
  color: { bg: string; text: string };
  icon: string;
}

// --- Finder Module ---

export interface FinderTreeNode {
  name: string;
  path: string;
  has_children: boolean;
}

export interface FinderListItem {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified: string;
  mime_type: string | null;
  extension: string | null;
}

export interface FinderListResponse {
  items: FinderListItem[];
  path: string;
  parent: string | null;
}

// --- Merge Conflict Detection ---

export interface MergeConflictEntry {
  task_id: string;
  pr_created_at: string;
  merge_position: number;
  can_merge: boolean;
  blocked_by: string | null;
}

export interface MergeConflictGroup {
  migration_number: number;
  tasks: MergeConflictEntry[];
}

export interface MergeConflictResponse {
  conflicts: MergeConflictGroup[];
}

// --- Pull Request Workflow ---

export type PrStatus = "draft" | "open" | "merging" | "merged" | "closed";

export interface PrDiff {
  stats: { additions: number; deletions: number; files_changed: number };
  unified_diff: string;
  is_empty: boolean;
}

export interface PullRequest {
  id: string;
  task_id: string;
  project: string;
  branch: string;
  target: string;
  status: PrStatus;
  title: string | null;
  body: string | null;
  worktree_path: string | null;
  closed_reason: string | null;
  diff: PrDiff | null;
  merged_at: string | null;
  created_at: string;
  approved_by: string | null;
  approved_at: string | null;
  submitted_by: string | null;
}

export interface FinderFileContent {
  content: string;
  filename: string;
  path: string;
  size: number;
  mime_type: string | null;
  encoding: "utf-8" | "base64";
  readonly: boolean;
}

// --- Billing Summary ---

export interface ProjectBillingSummary {
  project_slug: string;
  from_date: string;
  to_date: string;
  total_cost_usd: number;
  total_bill_usd: number;
  agent_cost_usd: number;
  human_cost_usd: number;
  billable_usd: number;
  non_billable_usd: number;
  task_count: number;
  entry_count: number;
  token_markup_factor: number;
  agent_bill_rate: number;
  human_bill_rate: number;
}

// --- Task Cost Tracking ---

export interface TaskCostEntry {
  id: string;
  task_id: string;
  entry_type: "agent" | "human";
  source: "task_completed" | "manual";
  conversation_id: string | null;
  pr_id: string | null;
  cost_usd_delta: number;
  agent_seconds: number;
  human_minutes: number;
  total_cost_usd: number;
  total_bill_usd: number;
  is_billable: boolean;
  billable_reason: string | null;
  description: string | null;
  created_by: string;
  created_at: string;
}

export interface TaskCostSummary {
  task_id: string;
  total_cost_usd: number;
  total_bill_usd: number;
  agent_cost_usd: number;
  human_cost_usd: number;
  billable_usd: number;
  non_billable_usd: number;
  entry_count: number;
  entries: TaskCostEntry[];
  created_entry_id: string | null;
}

// --- Automations (n8n) ---

export interface N8nWorkflow {
  id: string;
  name: string;
  active: boolean;
  createdAt?: string;
  updatedAt?: string;
}

export interface N8nExecution {
  id: string;
  workflowId?: string;
  status: string;
  startedAt?: string;
  stoppedAt?: string;
  finished?: boolean;
}

export interface OutboxEvent {
  id: number;
  event_type: string;
  project: string | null;
  actor_id: string | null;
  target_type: string | null;
  target_id: string | null;
  dispatched_at: string | null;
  retry_count: number;
  created_at: string;
}

// --- Notifications ---

export type NotificationType =
  | "task_pending"
  | "task_auto_approved"
  | "pr_submitted"
  | "task_completed"
  | "deploy_failed"
  | "deploy_success"
  | "task_zombie_report";

export interface Notification {
  id: string;
  user_id: string;
  event_id: string | null;
  type: NotificationType;
  title: string;
  body: string | null;
  target_type: "task" | "pr";
  target_id: string;
  project: string | null;
  read_at: string | null;
  acted_at: string | null;
  created_at: string;
}

// Anti-zombie D: body JSON payload for task_zombie_report notifications.
// Emitted by POST /api/v1/tasks/zombie-scan (weekly systemd timer), consumed
// by NotificationItem action button that calls POST /api/v1/tasks/bulk-reject.
// Not exported — callers only need parseZombieReportBody's inferred return type.
interface ZombieReportBody {
  project: string;
  count: number;
  threshold_days?: number;
  task_ids: string[];
  samples?: Array<{ id: string; title: string; age_days: number }>;
}

export function parseZombieReportBody(body: string | null): ZombieReportBody | null {
  if (!body) return null;
  try {
    const parsed: unknown = JSON.parse(body);
    if (!parsed || typeof parsed !== "object") return null;
    const obj = parsed as Record<string, unknown>;
    if (!Array.isArray(obj.task_ids) || obj.task_ids.length === 0) return null;
    if (!obj.task_ids.every((id): id is string => typeof id === "string")) return null;
    if (typeof obj.project !== "string" || typeof obj.count !== "number") return null;
    return obj as unknown as ZombieReportBody;
  } catch {
    return null;
  }
}

// --- CI Checks (Enterprise Phase 3) ---

export type CICheckStatus = "queued" | "in_progress" | "completed";
export type CICheckConclusion = "success" | "failure" | "neutral" | "cancelled" | "skipped" | "timed_out" | "action_required" | null;

export interface CICheck {
  id: string;
  task_id: string;
  check_name: string;
  status: CICheckStatus;
  conclusion: CICheckConclusion;
  details_url: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface CIChecksSummary {
  task_id: string;
  total: number;
  passed: number;
  failed: number;
  pending: number;
  merge_blocked: boolean;
  required_failing: string[];
}

// --- SSO Config (Enterprise Phase 2) ---

export interface SSOConfig {
  enabled: boolean;
  email_domains: string[];
  provider: string | null;
}

export interface SSOCallbackResult {
  success: boolean;
  error?: string;
}

// --- Workspace (Enterprise Phase 4) ---

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  sso_enabled: boolean;
  member_count: number;
}

export interface WorkspaceInvite {
  email: string;
  role: SystemRole;
}

export interface SSOWorkspaceConfig {
  enabled: boolean;
  email_domains: string[];
  provider: string | null;
  workspace_id: string;
}

// --- Audit Log (Enterprise Phase 5) ---

export type AuditEventType =
  | "login" | "logout"
  | "role_changed" | "team_joined" | "team_left"
  | "user_invited" | "sso_configured"
  | "workspace_created" | "task_completed" | "pr_merged";

export interface AuditLogEntry {
  id: string;
  timestamp: string;
  user_id: string;
  user_name: string;
  event_type: AuditEventType;
  description: string;
  metadata: Record<string, unknown> | null;
}

export interface AuditLogResponse {
  entries: AuditLogEntry[];
  next_cursor: string | null;
  total: number;
}

// --- Monitoring Module ---

export interface MetricDatapoint {
  t: number;
  v: number;
}

export interface CandleDatapoint {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
}

export interface ContainerMetrics {
  name: string;
  status: string;
  cpu_pct: number;
  memory_mb: number;
  memory_limit_mb: number;
  memory_pct: number;
  restart_count: number;
  uptime_seconds: number;
}

export interface ServiceStatus {
  name: string;
  status: string;
  details: string | null;
}

export interface ConnectivityStatus {
  tailscale: string;
  tailscale_ip: string | null;
  cf_tunnel: string;
}

export interface SecurityEvent {
  timestamp: number;
  event_type: string;
  source_ip: string | null;
  username: string | null;
  details: Record<string, unknown> | null;
}

export interface SSHSummary {
  success_count: number;
  failed_count: number;
  unique_ips: number;
}

export interface BanInfo {
  ip: string;
  jail: string;
  timestamp: number;
}

export interface AlertInfo {
  metric: string;
  value: number;
  threshold: number;
  level: string;
}

export interface SystemMetrics {
  cpu_pct: number;
  ram_pct: number;
  ram_used_mb: number;
  ram_total_mb: number;
  disk_pct: number;
  disk_used_gb: number;
  disk_total_gb: number;
  load_1m: number;
  load_5m: number;
  load_15m: number;
  uptime_seconds: number;
  net_rx_bps: number;
  net_tx_bps: number;
  cpu_count: number;
}

export interface SecuritySummary {
  ssh_success_24h: number;
  ssh_failed_24h: number;
  bans_active: number;
}

export interface MonitoringSnapshot {
  timestamp: number;
  system: SystemMetrics;
  docker: ContainerMetrics[];
  network: ConnectivityStatus;
  services: ServiceStatus[];
  alerts: AlertInfo[];
  sparklines: Record<string, MetricDatapoint[]>;
  security_summary: SecuritySummary;
}

export interface DiskTreeNode {
  path: string;
  name: string;
  size_mb: number;
  depth: number;
}

export interface DiskTreeResponse {
  items: DiskTreeNode[];
  total_mb: number;
  free_mb: number;
}

export interface SecurityData {
  ssh_events: SecurityEvent[];
  ssh_summary_24h: SSHSummary;
  active_bans: BanInfo[];
  ban_count_24h: number;
  console_logins: SecurityEvent[];
}

export type InboxDecision =
  | "ignore"
  | "keep"
  | "needs_human_review"
  | "create_idea"
  | "create_task";

export type InboxTopic =
  | "ai-news"
  | "ai-products"
  | "tooling"
  | "security-devtools"
  | "pv-energy"
  | "strategy-business"
  | "policy-politics"
  | "general";

export type InboxTreatment = "read" | "save" | "read_save" | "ignore";

export interface InboxTriageDecision {
  inbox_item_id: string;
  decision: InboxDecision;
  confidence: number;
  reason: string;
  target_program: string | null;
  target_project: string | null;
  task_kind: "idea" | "normal" | null;
  task_title: string | null;
  task_description: string | null;
  linked_task_id: string | null;
  tags: string[];
  decided_by: string;
  created_at: string;
  updated_at: string;
}

export type InboxStatus = "unread" | "read" | "saved" | "idea" | "newsletter" | "preferred" | "auto_ignored" | "ignored";
export type InboxIgnoreReason = "duplicate" | "spam" | "not_interested" | "not_relevant" | "custom";

export interface InboxItemSummary {
  id: string;
  source_type: string | null;
  source_label: string | null;
  external_id: string | null;
  title: string | null;
  snippet: string | null;
  sender: string | null;
  url: string | null;
  program: string | null;
  project: string | null;
  topic: InboxTopic;
  treatment: InboxTreatment;
  status: InboxStatus;
  ignore_reason: InboxIgnoreReason | null;
  received_at: string | null;
  needs_triage: boolean;
  triage: InboxTriageDecision | null;
}

export interface InboxItemDetail extends InboxItemSummary {
  content: string | null;
  raw_payload: unknown | null;
  tldr: string | null;
  deep_research: string | null;
}

export interface InboxStats {
  total: number;
  ideas: number;
  tasks: number;
  review: number;
  unread: number;
  read: number;
  saved: number;
  ignored: number;
}

export type IngestPendingStatus =
  | "queued"
  | "parser_waiting"
  | "parsing"
  | "classified"
  | "awaiting_triage"
  | "approved"
  | "inserted"
  | "done"
  | "parse_error"
  | "rejected";

export interface IngestPendingItem {
  id: string;
  file_path: string;
  project_slug: string;
  source_kind: string;
  mime_type: string | null;
  file_size_bytes: number | null;
  parser_used: string | null;
  extracted_text: string | null;
  structure: Record<string, unknown> | null;
  classification: {
    type?: string;
    title?: string | null;
    tags?: string[];
    target_folder?: string;
    target_filename?: string;
    confidence?: number;
    reason?: string;
    auto_approve?: boolean;
    rules_matched?: string[];
    learning_candidate?: unknown;
    task_candidate?: unknown;
    suggested_project_slug?: string;
    llm_metadata?: {
      model?: string;
      provider?: string;
      status?: string;
      reason?: string;
      project_slug?: string;
      valid_slug?: string | null;
      document_type?: string;
      title?: string;
      tags?: string[];
      confidence?: number;
      llm_confidence?: number;
      composite_confidence?: number;
      reasoning?: string;
      auto_approved?: boolean;
      auto_approve_blocked_reason?: string;
      auto_rejected?: boolean;
      auto_reject_reason?: string;
      existing_ingest_id?: string;
      source_project_slug?: string;
      source_project_prior?: number;
      source_project_reason?: string;
      source_project_followed?: boolean;
      source_project_overridden?: boolean;
    } | null;
  } | null;
  status: IngestPendingStatus;
  error_message: string | null;
  target_folder: string | null;
  target_filename: string | null;
  created_at: string;
  updated_at: string;
}

export interface IngestDecisionResponse {
  id: string;
  status: IngestPendingStatus;
}

export interface IngestUploadSkipped {
  path: string;
  reason: string;
}

export interface IngestUploadDedup {
  path: string;
  existing_ingest_id: string;
}

export interface IngestUploadResponse {
  project_slug: string;
  uploaded_files: number;
  queued_items: number;
  skipped_files: IngestUploadSkipped[];
  dedup_files: IngestUploadDedup[];
}

export type IngestSkipReason =
  | "dedup_sha256"
  | "invalid_path"
  | "mime_not_allowed"
  | "parse_error_pre_dispatch";

export interface IngestSkipEntry {
  id: string;
  file_path_attempted: string;
  project_slug: string;
  sha256: string | null;
  reason: IngestSkipReason;
  existing_ingest_id: string | null;
  error_message: string | null;
  created_at: string;
  created_by: string | null;
}

export type IngestHistoryDecision =
  | "auto_approved"
  | "auto_rejected"
  | "manual_approved"
  | "manual_rejected"
  | "parse_error"
  | "skipped";

export interface IngestHistoryEntry {
  id: string;
  source: "ingest_pending" | "ingest_skipped";
  decision: IngestHistoryDecision;
  status: string;
  file_path: string;
  filename: string;
  project_slug: string;
  mime_type: string | null;
  file_size_bytes: number | null;
  parser_used: string | null;
  document_type: string | null;
  confidence: number | null;
  target_folder: string | null;
  target_filename: string | null;
  reason: string | null;
  triage_decision_id: string | null;
  existing_ingest_id: string | null;
  created_at: string;
  updated_at: string;
}

export type ClassificationProposal = NonNullable<IngestPendingItem["classification"]>;
export type IngestPending = IngestPendingItem;

// --- Inbox Sources (PR D/5) ---
export type SourceType = "rss" | "email" | "manual" | "api" | "legacy";

export type SourceMetricsRange = "24h" | "7d" | "30d" | "total";

/**
 * Represents an inbox source (RSS feed, email alias, manual, etc.).
 *
 * The list/detail backend endpoints return aggregate metrics flat alongside
 * source fields. We mirror that shape at the API boundary and normalise
 * `active` (SQLite 0/1) to a real boolean in lib/api.ts.
 */
export interface InboxSource {
  id: string;
  name: string;
  source_key: string;
  feed_url: string | null;
  source_type: SourceType;
  active: boolean;
  last_fetch_at: string | null;
  last_fetch_error: string | null;
  workspace_id?: string;
  created_at: string;
  updated_at: string;
  // Aggregate metrics (from list_sources JOIN)
  total_items: number;
  unread_count: number;
  auto_ignored_count: number;
  score: number;
  upvotes: number;
  downvotes: number;
  reads: number;
}

/**
 * Detailed metrics returned by GET /inbox/sources/{id}/metrics?range=...
 * Uses a different, status-indexed shape than the list/detail aggregate.
 */
export interface InboxSourceMetrics {
  source_id: string;
  source_key: string;
  range: SourceMetricsRange;
  cutoff: string | null;
  total: number;
  unread: number;
  read: number;
  saved: number;
  newsletter: number;
  preferred: number;
  idea: number;
  ignored: number;
  auto_ignored: number;
  score: number;
  upvotes: number;
  downvotes: number;
  reads: number;
}

// --- Newsletter ---
export interface NewsletterItem {
  id: string;
  title: string;
  source_domain: string;
  deep_research_clean: string;
  url: string;
  topic: string;
}

export interface NewsletterRubrica {
  name: string;
  color: string;
  items: NewsletterItem[];
}

export interface NewsletterRecipient {
  id: string;
  email: string;
  name: string | null;
  active: boolean;
}
