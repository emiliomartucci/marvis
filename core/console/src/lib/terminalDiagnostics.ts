const DIAGNOSTICS_STATE_KEY = "pir-terminal-diagnostics-state";
const DIAGNOSTICS_EVENTS_KEY = "pir-terminal-diagnostics-events";
const DIAGNOSTICS_UPLOADED_IDS_KEY = "pir-terminal-diagnostics-uploaded-ids";
const DIAGNOSTICS_DROPPED_KEY = "pir-terminal-diagnostics-dropped-events";
const DIAGNOSTICS_CHANGE_EVENT = "pir-terminal-diagnostics-change";
const DIAGNOSTICS_WINDOW_MS = 30 * 60 * 1000;
const MAX_EVENTS = 5000;
const COUNTER_WINDOW_MS = 1000;
const MAX_COUNTER_SAMPLES = 600;
const COUNTER_EVENT_FLUSH_MS = 1000;

type JsonObject = Record<string, unknown>;

export type TerminalInputKind = "text" | "enter" | "sgr" | "paste" | "control";

export type TerminalCounterName =
  | "bytes_received_per_sec"
  | "input_bytes_sent"
  | "parse_ms"
  | "api_fetch_rtt_ms"
  | "api_probe_download_bytes"
  | "browser_snapshot_bytes"
  | "browser_snapshot_count"
  | "browser_snapshot_evictions"
  | "server_internet_probe_ms"
  | "websocket_ping_age_ms"
  | "send_recv_latency_ms"
  | "wheel_events_per_sec"
  | "wheel_events_pre_coalesce"
  | "wheel_events_post_coalesce"
  | "hidden_buffered_bytes"
  | "mounted_terminal_count"
  | "hot_terminal_count"
  | "cold_terminal_count";

export type TerminalDiagnosticEventType =
  | "active_session_metrics_refresh_failed"
  | "active_session_metrics_refreshed"
  | "active_session_select"
  | "active_session_url_updated"
  | "browser_blur"
  | "browser_file_upload_denied"
  | "browser_focus"
  | "browser_pagehide"
  | "browser_pageshow"
  | "browser_visibility_change"
  | "diagnostics_started"
  | "diagnostics_stopped"
  | "diagnostics_toolbar_started_with_context"
  | "manual_mark"
  | "restore_uuid_resolution_failed"
  | "route_session_name_check_failed"
  | "route_session_name_not_found"
  | "route_session_param_detected"
  | "route_target_detected"
  | "route_terminal_root_restored"
  | "route_uuid_resolution_failed"
  | "route_uuid_resolved"
  | "session_created"
  | "session_deleted"
  | "session_metadata_remembered"
  | "session_renamed"
  | "sessions_count_changed"
  | "sessions_delta_applied"
  | "sessions_delta_missing_session"
  | "sessions_rename_delta_applied"
  | "sessions_rename_delta_dedup"
  | "sessions_rename_delta_invalid"
  | "terminal_hidden_frame_dropped"
  | "terminal_hidden_ring_flushed"
  | "sessions_fetch_coalesced"
  | "sessions_fetch_deferred_hidden"
  | "sessions_fetch_dirty_deferred"
  | "sessions_fetch_dirty_followup"
  | "sessions_fetch_dirty_skipped_recent"
  | "sessions_fetch_outdated_ignored"
  | "sessions_fetch_failed"
  | "sessions_fetch_started"
  | "sessions_fetch_structural_followup"
  | "sessions_fetch_succeeded"
  | "terminal_cold_to_hot_cancelled"
  | "terminal_cold_to_hot_completed"
  | "terminal_cold_to_hot_failed"
  | "terminal_cold_to_hot_started"
  | "terminal_cold_pty_ready"
  | "terminal_cold_snapshot_capture_skipped"
  | "terminal_cold_snapshot_captured"
  | "terminal_cold_snapshot_deleted"
  | "terminal_cold_snapshot_missed"
  | "terminal_cold_snapshot_painted"
  | "terminal_cold_snapshot_renamed"
  | "terminal_cold_snapshot_stats"
  | "terminal_cold_ws_connected"
  | "terminal_hot_cold_counts"
  | "terminal_hot_session_demoted"
  | "terminal_hot_session_promoted"
  | "terminal_active_visible"
  | "terminal_auth_error_redirect"
  | "terminal_clipboard_read_failed"
  | "terminal_clipboard_read_image"
  | "terminal_document_visibility"
  | "terminal_drop_files"
  | "terminal_metrics_fetch_failed"
  | "terminal_metrics_fetched"
  | "terminal_metrics_batch_post_failed"
  | "terminal_metrics_batch_posted"
  | "terminal_mount"
  | "terminal_network_probe"
  | "terminal_network_probe_failed"
  | "terminal_panel_mount"
  | "terminal_panel_state"
  | "terminal_panel_unmount"
  | "terminal_paste_file_detected"
  | "terminal_perf_input"
  | "terminal_perf_output"
  | "terminal_perf_parse"
  | "terminal_perf_send_recv_latency"
  | "terminal_perf_wheel_burst"
  | "terminal_resize_deferred_unstable"
  | "terminal_resize_observed"
  | "terminal_resize_sent"
  | "terminal_snapshot_reconnect_forced"
  | "terminal_snapshot_reconnect_scheduled"
  | "terminal_sync_applied"
  | "terminal_sync_scheduled"
  | "terminal_theme_updated"
  | "terminal_unmount"
  | "terminal_upload_button_files_selected"
  | "terminal_upload_event_received"
  | "terminal_upload_failed"
  | "terminal_upload_started"
  | "terminal_upload_succeeded"
  | "terminal_window_focus"
  | "terminal_ws_lifecycle"
  | "terminal_ws_status";

export interface TerminalCounterSummary {
  name: TerminalCounterName;
  isActive: boolean;
  sessionName: string | null;
  kind: TerminalInputKind | null;
  count: number;
  sum: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
  max: number | null;
  windowMs: number;
}

interface DiagnosticsState {
  runId: string;
  enabledAt: number;
  expiresAt: number;
  reason: string;
}

export interface TerminalDiagnosticEvent {
  id: string;
  runId: string;
  ts: string;
  tsMs: number;
  type: string;
  route: string;
  hidden: boolean;
  payload: JsonObject;
}

export interface TerminalDiagnosticsInfo {
  active: boolean;
  startedAt: number | null;
  expiresAt: number | null;
  remainingMs: number;
  eventCount: number;
  droppedEventCount: number;
  counters: TerminalCounterSummary[];
}

export type TerminalTelemetryErrorKind = "auth" | "network" | "http" | "abort" | "unknown";

export interface TerminalDiagnosticsTelemetryHealth {
  runId: string | null;
  totalEvents: number;
  uploadedEvents: number;
  pendingEvents: number;
  droppedEvents: number;
  visibility: {
    hiddenMs: number;
    visibleMs: number;
    hiddenTransitions: number;
    currentlyHidden: boolean;
  };
  upload: {
    postedBatches: number;
    failedBatches: number;
    authFailures: number;
    networkFailures: number;
    httpFailures: number;
    otherFailures: number;
    lastSuccessAt: string | null;
    lastFailureAt: string | null;
    lastFailureKind: TerminalTelemetryErrorKind | null;
    lastFailureMessage: string | null;
  };
  metricsFetch: {
    failures: number;
    authFailures: number;
    networkFailures: number;
    httpFailures: number;
    otherFailures: number;
    lastFailureAt: string | null;
    lastFailureKind: TerminalTelemetryErrorKind | null;
    lastFailureMessage: string | null;
  };
  networkProbe: {
    failures: number;
    authFailures: number;
    networkFailures: number;
    httpFailures: number;
    otherFailures: number;
    lastFailureAt: string | null;
    lastFailureKind: TerminalTelemetryErrorKind | null;
    lastFailureMessage: string | null;
  };
}

interface CounterSample {
  tsMs: number;
  value: number;
  isActive: boolean;
  sessionName: string | null;
  kind: TerminalInputKind | null;
}

const COUNTER_EVENT_TYPES: Record<TerminalCounterName, TerminalDiagnosticEventType> = {
  api_fetch_rtt_ms: "terminal_network_probe",
  api_probe_download_bytes: "terminal_network_probe",
  browser_snapshot_bytes: "terminal_cold_snapshot_stats",
  browser_snapshot_count: "terminal_cold_snapshot_stats",
  browser_snapshot_evictions: "terminal_cold_snapshot_stats",
  bytes_received_per_sec: "terminal_perf_output",
  hidden_buffered_bytes: "terminal_perf_output",
  cold_terminal_count: "terminal_hot_cold_counts",
  hot_terminal_count: "terminal_hot_cold_counts",
  input_bytes_sent: "terminal_perf_input",
  mounted_terminal_count: "terminal_hot_cold_counts",
  parse_ms: "terminal_perf_parse",
  server_internet_probe_ms: "terminal_network_probe",
  send_recv_latency_ms: "terminal_perf_send_recv_latency",
  websocket_ping_age_ms: "terminal_network_probe",
  wheel_events_per_sec: "terminal_perf_wheel_burst",
  wheel_events_pre_coalesce: "terminal_perf_wheel_burst",
  wheel_events_post_coalesce: "terminal_perf_wheel_burst",
};

const counterSamples = new Map<string, CounterSample[]>();
const counterLastFlushedAt = new Map<string, number>();

function isBrowser() {
  return typeof window !== "undefined";
}

function readJson<T>(key: string, fallback: T): T {
  if (!isBrowser()) return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? JSON.parse(raw) as T : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown) {
  if (!isBrowser()) return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // ignore storage failures
  }
}

function emitDiagnosticsChange() {
  if (!isBrowser()) return;
  window.dispatchEvent(new CustomEvent(DIAGNOSTICS_CHANGE_EVENT));
}

function getRoute() {
  if (!isBrowser()) return "";
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

function newId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `diag-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function counterKey(sample: Omit<CounterSample, "tsMs" | "value"> & { name: TerminalCounterName }) {
  return [
    sample.name,
    sample.isActive ? "active" : "hidden",
    sample.sessionName ?? "",
    sample.kind ?? "",
  ].join("|");
}

function percentile(values: number[], p: number) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.max(0, Math.min(sorted.length - 1, Math.round((sorted.length - 1) * p)));
  return sorted[index];
}

function summarizeCounter(
  key: string,
  name: TerminalCounterName,
  nowMs: number,
): TerminalCounterSummary | null {
  const samples = counterSamples.get(key);
  if (!samples || samples.length === 0) return null;
  const recent = samples.filter((sample) => nowMs - sample.tsMs <= COUNTER_WINDOW_MS);
  counterSamples.set(key, recent.slice(-MAX_COUNTER_SAMPLES));
  if (recent.length === 0) return null;
  const values = recent.map((sample) => sample.value);
  const first = recent[0];
  return {
    name,
    isActive: first.isActive,
    sessionName: first.sessionName,
    kind: first.kind,
    count: recent.length,
    sum: values.reduce((total, value) => total + value, 0),
    p50: percentile(values, 0.50),
    p95: percentile(values, 0.95),
    p99: percentile(values, 0.99),
    max: Math.max(...values),
    windowMs: COUNTER_WINDOW_MS,
  };
}

function getCounterSummaries() {
  const nowMs = Date.now();
  const summaries: TerminalCounterSummary[] = [];
  for (const [key] of counterSamples) {
    const [name] = key.split("|") as [TerminalCounterName];
    const summary = summarizeCounter(key, name, nowMs);
    if (summary) summaries.push(summary);
  }
  return summaries.sort((a, b) => {
    if (a.sessionName !== b.sessionName) return (a.sessionName ?? "").localeCompare(b.sessionName ?? "");
    if (a.name !== b.name) return a.name.localeCompare(b.name);
    if (a.isActive !== b.isActive) return a.isActive ? -1 : 1;
    return (a.kind ?? "").localeCompare(b.kind ?? "");
  });
}

function readState() {
  return readJson<DiagnosticsState | null>(DIAGNOSTICS_STATE_KEY, null);
}

function writeState(state: DiagnosticsState | null) {
  if (!isBrowser()) return;
  if (state) {
    writeJson(DIAGNOSTICS_STATE_KEY, state);
  } else {
    window.localStorage.removeItem(DIAGNOSTICS_STATE_KEY);
  }
}

function readEvents() {
  return readJson<TerminalDiagnosticEvent[]>(DIAGNOSTICS_EVENTS_KEY, []);
}

function writeEvents(events: TerminalDiagnosticEvent[]) {
  const dropped = Math.max(0, events.length - MAX_EVENTS);
  if (dropped > 0) {
    writeJson(DIAGNOSTICS_DROPPED_KEY, readDroppedEventCount() + dropped);
  }
  writeJson(DIAGNOSTICS_EVENTS_KEY, events.slice(-MAX_EVENTS));
}

function readDroppedEventCount() {
  return readJson<number>(DIAGNOSTICS_DROPPED_KEY, 0);
}

function readUploadedIds() {
  return new Set(readJson<string[]>(DIAGNOSTICS_UPLOADED_IDS_KEY, []));
}

function writeUploadedIds(ids: Set<string>) {
  writeJson(DIAGNOSTICS_UPLOADED_IDS_KEY, [...ids].slice(-10000));
}

function payloadString(payload: JsonObject, key: string) {
  const value = payload[key];
  return typeof value === "string" ? value : null;
}

function payloadErrorKind(payload: JsonObject): TerminalTelemetryErrorKind | null {
  const value = payload.errorKind;
  if (
    value === "auth" ||
    value === "network" ||
    value === "http" ||
    value === "abort" ||
    value === "unknown"
  ) {
    return value;
  }
  return null;
}

export function classifyTerminalTelemetryError(error: unknown): TerminalTelemetryErrorKind {
  const maybeStatus = error && typeof error === "object" && "status" in error
    ? (error as { status?: unknown }).status
    : null;
  if (maybeStatus === 401) return "auth";
  if (typeof maybeStatus === "number") return "http";

  const message = error instanceof Error ? error.message : String(error);
  if (/abort/i.test(message)) return "abort";
  if (/unauthorized|\\b401\\b/i.test(message)) return "auth";
  if (/failed to fetch|network|load failed|internet|offline/i.test(message)) return "network";
  return "unknown";
}

function normalizeState() {
  const state = readState();
  if (!state) return null;
  if (state.expiresAt > Date.now()) return state;
  writeState(null);
  emitDiagnosticsChange();
  return null;
}

export function getTerminalDiagnosticsInfo(): TerminalDiagnosticsInfo {
  const state = normalizeState();
  const eventCount = readEvents().length;
  return {
    active: Boolean(state),
    startedAt: state?.enabledAt ?? null,
    expiresAt: state?.expiresAt ?? null,
    remainingMs: state ? Math.max(0, state.expiresAt - Date.now()) : 0,
    eventCount,
    droppedEventCount: readDroppedEventCount(),
    counters: getCounterSummaries(),
  };
}

export function bootTerminalDiagnosticsFromLocation() {
  if (!isBrowser()) return;
  const params = new URLSearchParams(window.location.search);
  const mode = params.get("terminalDebug");
  if (mode === "1" && !normalizeState()) {
    startTerminalDiagnostics("query-param");
  }
  if (mode === "0") {
    stopTerminalDiagnostics("query-param");
  }
}

export function startTerminalDiagnostics(reason = "manual") {
  const state: DiagnosticsState = {
    runId: newId(),
    enabledAt: Date.now(),
    expiresAt: Date.now() + DIAGNOSTICS_WINDOW_MS,
    reason,
  };
  writeState(state);
  writeEvents([]);
  writeJson(DIAGNOSTICS_DROPPED_KEY, 0);
  writeUploadedIds(new Set());
  counterSamples.clear();
  counterLastFlushedAt.clear();
  emitDiagnosticsChange();
  recordTerminalDiagnosticEvent("diagnostics_started", {
    reason,
    userAgent: typeof navigator !== "undefined" ? navigator.userAgent : "",
  });
  return getTerminalDiagnosticsInfo();
}

export function stopTerminalDiagnostics(reason = "manual") {
  const state = normalizeState();
  if (state) {
    appendEvent(state, {
      type: "diagnostics_stopped",
      payload: { reason },
    });
  }
  writeState(null);
  emitDiagnosticsChange();
  return getTerminalDiagnosticsInfo();
}

export function clearTerminalDiagnostics() {
  if (!isBrowser()) return;
  window.localStorage.removeItem(DIAGNOSTICS_STATE_KEY);
  window.localStorage.removeItem(DIAGNOSTICS_EVENTS_KEY);
  window.localStorage.removeItem(DIAGNOSTICS_UPLOADED_IDS_KEY);
  window.localStorage.removeItem(DIAGNOSTICS_DROPPED_KEY);
  counterSamples.clear();
  counterLastFlushedAt.clear();
  emitDiagnosticsChange();
}

function appendEvent(
  state: DiagnosticsState,
  entry: { type: TerminalDiagnosticEventType; payload?: JsonObject },
) {
  const event: TerminalDiagnosticEvent = {
    id: newId(),
    runId: state.runId,
    ts: new Date().toISOString(),
    tsMs: Date.now(),
    type: entry.type,
    route: getRoute(),
    hidden: typeof document !== "undefined" ? document.hidden : false,
    payload: entry.payload ?? {},
  };
  const events = readEvents();
  events.push(event);
  writeEvents(events.filter((item) => item.runId === state.runId || item.tsMs >= state.enabledAt));
  if (typeof console !== "undefined") {
    console.debug("[terminal-diag]", event.type, event);
  }
}

export function recordTerminalDiagnosticEvent(type: TerminalDiagnosticEventType, payload: JsonObject = {}) {
  const state = normalizeState();
  if (!state) return false;
  appendEvent(state, { type, payload });
  return true;
}

export function recordCounterSample(
  name: TerminalCounterName,
  value: number,
  isActive: boolean,
  metadata: { sessionName?: string; kind?: TerminalInputKind } = {},
) {
  const state = normalizeState();
  if (!state) return false;

  const key = counterKey({
    name,
    isActive,
    sessionName: metadata.sessionName ?? null,
    kind: metadata.kind ?? null,
  });
  const samples = counterSamples.get(key) ?? [];
  samples.push({
    tsMs: Date.now(),
    value,
    isActive,
    sessionName: metadata.sessionName ?? null,
    kind: metadata.kind ?? null,
  });
  counterSamples.set(key, samples.slice(-MAX_COUNTER_SAMPLES));

  const nowMs = Date.now();
  const lastFlushedAt = counterLastFlushedAt.get(key) ?? 0;
  if (nowMs - lastFlushedAt >= COUNTER_EVENT_FLUSH_MS) {
    counterLastFlushedAt.set(key, nowMs);
    const summary = summarizeCounter(key, name, nowMs);
    if (summary) {
      appendEvent(state, {
        type: COUNTER_EVENT_TYPES[name],
        payload: summary as unknown as JsonObject,
      });
    }
    emitDiagnosticsChange();
  }
  return true;
}

export function markTerminalDiagnostic(note: string, payload: JsonObject = {}) {
  return recordTerminalDiagnosticEvent("manual_mark", {
    note,
    ...payload,
  });
}

export function getBrowserNetworkInfo(): JsonObject {
  if (!isBrowser()) return {};
  const nav = navigator as Navigator & {
    connection?: {
      downlink?: number;
      effectiveType?: string;
      rtt?: number;
      saveData?: boolean;
    };
    deviceMemory?: number;
  };
  return {
    userAgent: navigator.userAgent,
    hardwareConcurrency: navigator.hardwareConcurrency ?? null,
    deviceMemoryGb: nav.deviceMemory ?? null,
    connection: nav.connection
      ? {
          downlinkMbps: nav.connection.downlink ?? null,
          effectiveType: nav.connection.effectiveType ?? null,
          rttMs: nav.connection.rtt ?? null,
          saveData: nav.connection.saveData ?? null,
        }
      : null,
  };
}

function incrementFailureBucket(
  target: {
    authFailures: number;
    networkFailures: number;
    httpFailures: number;
    otherFailures: number;
  },
  kind: TerminalTelemetryErrorKind,
) {
  if (kind === "auth") target.authFailures += 1;
  else if (kind === "network") target.networkFailures += 1;
  else if (kind === "http") target.httpFailures += 1;
  else target.otherFailures += 1;
}

function buildFailureSummary(events: TerminalDiagnosticEvent[], eventType: string) {
  const summary = {
    failures: 0,
    authFailures: 0,
    networkFailures: 0,
    httpFailures: 0,
    otherFailures: 0,
    lastFailureAt: null as string | null,
    lastFailureKind: null as TerminalTelemetryErrorKind | null,
    lastFailureMessage: null as string | null,
  };

  for (const event of events) {
    if (event.type !== eventType) continue;
    const kind = payloadErrorKind(event.payload)
      ?? classifyTerminalTelemetryError(payloadString(event.payload, "error") ?? "unknown");
    summary.failures += 1;
    incrementFailureBucket(summary, kind);
    summary.lastFailureAt = event.ts;
    summary.lastFailureKind = kind;
    summary.lastFailureMessage = payloadString(event.payload, "error");
  }

  return summary;
}

function buildVisibilitySummary(
  events: TerminalDiagnosticEvent[],
  endMs: number,
): TerminalDiagnosticsTelemetryHealth["visibility"] {
  if (events.length === 0) {
    return {
      hiddenMs: 0,
      visibleMs: 0,
      hiddenTransitions: 0,
      currentlyHidden: typeof document !== "undefined" ? document.hidden : false,
    };
  }

  let hidden = events[0].hidden;
  let cursorMs = events[0].tsMs;
  let hiddenMs = 0;
  let visibleMs = 0;
  let hiddenTransitions = 0;

  for (const event of events) {
    if (event.type !== "browser_visibility_change") continue;
    const nextHidden = event.payload.hidden;
    if (typeof nextHidden !== "boolean") continue;
    const elapsedMs = Math.max(0, event.tsMs - cursorMs);
    if (hidden) hiddenMs += elapsedMs;
    else visibleMs += elapsedMs;
    if (nextHidden) hiddenTransitions += 1;
    hidden = nextHidden;
    cursorMs = event.tsMs;
  }

  const tailMs = Math.max(0, endMs - cursorMs);
  if (hidden) hiddenMs += tailMs;
  else visibleMs += tailMs;

  return {
    hiddenMs,
    visibleMs,
    hiddenTransitions,
    currentlyHidden: hidden,
  };
}

export function getTerminalDiagnosticsTelemetryHealth(): TerminalDiagnosticsTelemetryHealth {
  const state = readState();
  const events = readEvents();
  const uploaded = readUploadedIds();
  const runId = state?.runId ?? events.at(-1)?.runId ?? null;
  const runEvents = runId ? events.filter((event) => event.runId === runId) : events;
  const uploadedEvents = runEvents.filter((event) => uploaded.has(event.id)).length;
  const uploadFailures = buildFailureSummary(runEvents, "terminal_metrics_batch_post_failed");
  const metricsFetchFailures = buildFailureSummary(runEvents, "terminal_metrics_fetch_failed");
  const networkProbeFailures = buildFailureSummary(runEvents, "terminal_network_probe_failed");
  const postedEvents = runEvents.filter((event) => event.type === "terminal_metrics_batch_posted");
  const lastPosted = postedEvents.at(-1);

  return {
    runId,
    totalEvents: runEvents.length,
    uploadedEvents,
    pendingEvents: Math.max(0, runEvents.length - uploadedEvents),
    droppedEvents: readDroppedEventCount(),
    visibility: buildVisibilitySummary(runEvents, state ? Date.now() : (runEvents.at(-1)?.tsMs ?? Date.now())),
    upload: {
      postedBatches: postedEvents.length,
      failedBatches: uploadFailures.failures,
      authFailures: uploadFailures.authFailures,
      networkFailures: uploadFailures.networkFailures,
      httpFailures: uploadFailures.httpFailures,
      otherFailures: uploadFailures.otherFailures,
      lastSuccessAt: lastPosted?.ts ?? null,
      lastFailureAt: uploadFailures.lastFailureAt,
      lastFailureKind: uploadFailures.lastFailureKind,
      lastFailureMessage: uploadFailures.lastFailureMessage,
    },
    metricsFetch: metricsFetchFailures,
    networkProbe: networkProbeFailures,
  };
}

export function getPendingTerminalDiagnosticsBatch(limit = 500) {
  const state = normalizeState();
  if (!state) return null;
  const uploaded = readUploadedIds();
  const events = readEvents()
    .filter((event) => event.runId === state.runId && !uploaded.has(event.id))
    .slice(0, limit);
  if (events.length === 0) return null;
  return {
    run_id: state.runId,
    source: "console",
    exported_at: new Date().toISOString(),
    events,
    counters: getCounterSummaries(),
    browser: getBrowserNetworkInfo(),
    telemetry_health: getTerminalDiagnosticsTelemetryHealth(),
  };
}

export function markTerminalDiagnosticsBatchPosted(eventIds: string[]) {
  if (!eventIds.length) return;
  const uploaded = readUploadedIds();
  for (const id of eventIds) uploaded.add(id);
  writeUploadedIds(uploaded);
}

export function getTerminalDiagnosticsExport() {
  return {
    exportedAt: new Date().toISOString(),
    info: getTerminalDiagnosticsInfo(),
    state: readState(),
    events: readEvents(),
    counters: getCounterSummaries(),
    browser: getBrowserNetworkInfo(),
    telemetry_health: getTerminalDiagnosticsTelemetryHealth(),
  };
}

export function downloadTerminalDiagnostics() {
  if (!isBrowser()) return;
  const blob = new Blob([
    JSON.stringify(getTerminalDiagnosticsExport(), null, 2),
  ], { type: "application/json" });
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `terminal-diagnostics-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

export function registerTerminalDiagnosticsConsole() {
  if (!isBrowser()) return;
  const api = {
    start: (reason?: string) => startTerminalDiagnostics(reason),
    stop: (reason?: string) => stopTerminalDiagnostics(reason),
    clear: () => clearTerminalDiagnostics(),
    dump: () => getTerminalDiagnosticsExport(),
    download: () => downloadTerminalDiagnostics(),
    mark: (note: string, payload?: JsonObject) => markTerminalDiagnostic(note, payload),
    info: () => getTerminalDiagnosticsInfo(),
  };
  (window as typeof window & { __pirTerminalDiagnostics?: typeof api }).__pirTerminalDiagnostics = api;
}

export function getTerminalDiagnosticsChangeEventName() {
  return DIAGNOSTICS_CHANGE_EVENT;
}
