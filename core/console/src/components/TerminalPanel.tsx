"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MutableRefObject,
} from "react";
import dynamic from "next/dynamic";
import TerminalRestoreOverlay, {
  type TerminalRestorePhase,
  type TerminalRestoreState,
} from "@/components/TerminalRestoreOverlay";
import {
  getSessionMetrics,
  getSessionByUUID,
  getTerminalMetrics,
  getTerminalNetworkProbe,
  listSessions,
  postTerminalMetricsBatch,
} from "@/lib/api";
import { redirectToConsoleLogin } from "@/lib/config";
import SessionSidebar from "@/components/SessionSidebar";
import CommandPalette from "@/components/CommandPalette";
import type {
  Session,
  SessionMetrics,
  SessionProvider,
  TerminalMetricsSnapshot,
  TerminalNetworkProbeResponse,
  WSConnectionStatus,
} from "@/lib/types";
import type { TerminalWSLifecycleEvent } from "@/lib/ws";
import { useDesignV2 } from "@/lib/useDesignV2";
import { dispatchSessionsCountChanged } from "@/lib/sessionEvents";
import { L5Loader } from "@/components/ui/L5Loader";
import {
  bootTerminalDiagnosticsFromLocation,
  classifyTerminalTelemetryError,
  downloadTerminalDiagnostics,
  getTerminalDiagnosticsChangeEventName,
  getTerminalDiagnosticsInfo,
  getPendingTerminalDiagnosticsBatch,
  markTerminalDiagnosticsBatchPosted,
  markTerminalDiagnostic,
  recordCounterSample,
  recordTerminalDiagnosticEvent,
  registerTerminalDiagnosticsConsole,
  startTerminalDiagnostics,
  stopTerminalDiagnostics,
  type TerminalCounterSummary,
} from "@/lib/terminalDiagnostics";

// --- Last session persistence ---

const LAST_SESSION_KEY = "marvis-last-terminal-session";

function saveLastSession(uuid: string | null, name: string) {
  try {
    localStorage.setItem(LAST_SESSION_KEY, JSON.stringify({ uuid, name }));
  } catch {}
}

function loadLastSession(): { uuid: string | null; name: string } | null {
  try {
    const raw = localStorage.getItem(LAST_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed?.name ? parsed : null;
  } catch {
    return null;
  }
}

function clearLastSession() {
  try {
    localStorage.removeItem(LAST_SESSION_KEY);
  } catch {}
}

const Terminal = dynamic(() => import("@/components/Terminal"), {
  ssr: false,
  loading: () => <div className="bg-pir-base h-full" />,
});

const MAX_HOT_TERMINALS_PER_BROWSER = 2;
const SESSION_REFRESH_COALESCE_MS = 150;
const SESSION_STRUCTURAL_DIRTY_FOLLOWUP_MS = 1_000;
const SESSION_STATE_DIRTY_FOLLOWUP_MS = 5_000;
const ACTIVE_SESSION_METRICS_REFRESH_MS = 30_000;
// Max wall-clock a cold->hot activation may stay pending before we treat it as
// failed (PTY never emitted / user switched away). Bounds "incomplete" started
// events that never reach completed/failed.
const COLD_TO_HOT_MAX_MS = 15_000;

type SessionActivityState = Session["activity_state"];

interface SessionsChangedDetail {
  type?: string;
  event?: string;
  session_name?: string;
  state?: unknown;
  // Plan 2026-05-21 — `event: "renamed"` carries delta payload
  old_name?: string;
  new_name?: string;
  session_info?: SessionRenameInfo;
}

interface SessionRenameInfo {
  name: string;
  prev_name: string;
  display_name?: string | null;
  provider?: SessionProvider | null;
  model?: string | null;
  project_slug?: string | null;
  status?: string | null;
  activity_state?: string | null;
  updated_at: string;
}

// Idempotency dedup: same renamed event received twice (e.g. issuer tab
// gets PATCH response THEN WS broadcast) must be applied once. Key =
// `${old}|${new}|${updated_at}`, TTL 60s, max 100 entries (LRU evict).
const RENAME_DEDUP_TTL_MS = 60_000;
const RENAME_DEDUP_MAX_ENTRIES = 100;

interface PendingSessionRefreshDirty {
  reason: string;
  structural: boolean;
}

interface DirtySessionRefreshContext {
  requestId: number;
  timerRef: MutableRefObject<ReturnType<typeof setTimeout> | null>;
  lastSuccessfulRefreshAtRef: MutableRefObject<number>;
  activeSessionRef: MutableRefObject<string | null>;
  openSessionsRef: MutableRefObject<string[]>;
  fetchSessionsNow: (reason: string) => Promise<void>;
}

interface SessionProcessSummary {
  sessionCount: number;
  measuredCount: number;
  workingCount: number;
  totalCpuPct: number;
  maxCpuPct: number | null;
  totalRamMb: number;
  maxRamMb: number | null;
  byProvider: Record<
    string,
    {
      count: number;
      measuredCount: number;
      workingCount: number;
      totalCpuPct: number;
      maxCpuPct: number | null;
      totalRamMb: number;
      maxRamMb: number | null;
    }
  >;
}

type ProviderProcessSummary = SessionProcessSummary["byProvider"][string];

function roundTenth(value: number) {
  return Math.round(value * 10) / 10;
}

function getProviderProcessSummary(
  summary: SessionProcessSummary,
  provider: string,
): ProviderProcessSummary {
  if (!summary.byProvider[provider]) {
    summary.byProvider[provider] = {
      count: 0,
      measuredCount: 0,
      workingCount: 0,
      totalCpuPct: 0,
      maxCpuPct: null,
      totalRamMb: 0,
      maxRamMb: null,
    };
  }
  return summary.byProvider[provider];
}

function addCpuSample(
  summary: SessionProcessSummary,
  providerSummary: ProviderProcessSummary,
  cpuPct: number | null,
) {
  if (typeof cpuPct !== "number") return;
  summary.totalCpuPct += cpuPct;
  providerSummary.totalCpuPct += cpuPct;
  summary.maxCpuPct = Math.max(summary.maxCpuPct ?? 0, cpuPct);
  providerSummary.maxCpuPct = Math.max(providerSummary.maxCpuPct ?? 0, cpuPct);
}

function addRamSample(
  summary: SessionProcessSummary,
  providerSummary: ProviderProcessSummary,
  ramMb: number | null,
) {
  if (typeof ramMb !== "number") return;
  summary.totalRamMb += ramMb;
  providerSummary.totalRamMb += ramMb;
  summary.maxRamMb = Math.max(summary.maxRamMb ?? 0, ramMb);
  providerSummary.maxRamMb = Math.max(providerSummary.maxRamMb ?? 0, ramMb);
}

function summarizeSessionProcess(sessions: Session[]): SessionProcessSummary {
  const summary: SessionProcessSummary = {
    sessionCount: sessions.length,
    measuredCount: 0,
    workingCount: 0,
    totalCpuPct: 0,
    maxCpuPct: null,
    totalRamMb: 0,
    maxRamMb: null,
    byProvider: {},
  };

  for (const session of sessions) {
    const provider = session.provider ?? "unknown";
    const providerSummary = getProviderProcessSummary(summary, provider);
    providerSummary.count += 1;
    if (session.activity_state === "working") {
      summary.workingCount += 1;
      providerSummary.workingCount += 1;
    }
    const hasCpu = typeof session.cpu_pct === "number";
    const hasRam = typeof session.ram_mb === "number";
    if (hasCpu || hasRam) {
      summary.measuredCount += 1;
      providerSummary.measuredCount += 1;
    }
    addCpuSample(summary, providerSummary, session.cpu_pct);
    addRamSample(summary, providerSummary, session.ram_mb);
  }

  summary.totalCpuPct = roundTenth(summary.totalCpuPct);
  summary.maxCpuPct = summary.maxCpuPct == null ? null : roundTenth(summary.maxCpuPct);
  summary.totalRamMb = roundTenth(summary.totalRamMb);
  summary.maxRamMb = summary.maxRamMb == null ? null : roundTenth(summary.maxRamMb);
  for (const providerSummary of Object.values(summary.byProvider)) {
    providerSummary.totalCpuPct = roundTenth(providerSummary.totalCpuPct);
    providerSummary.maxCpuPct =
      providerSummary.maxCpuPct == null ? null : roundTenth(providerSummary.maxCpuPct);
    providerSummary.totalRamMb = roundTenth(providerSummary.totalRamMb);
    providerSummary.maxRamMb =
      providerSummary.maxRamMb == null ? null : roundTenth(providerSummary.maxRamMb);
  }
  return summary;
}

function isSessionActivityState(value: unknown): value is Exclude<SessionActivityState, null> {
  return (
    value === "working" ||
    value === "idle" ||
    value === "needs_input" ||
    value === "active"
  );
}

function sessionsChangedDetail(event: Event): SessionsChangedDetail {
  return event instanceof CustomEvent && event.detail && typeof event.detail === "object"
    ? event.detail as SessionsChangedDetail
    : {};
}

function isStateOnlySessionsChanged(
  detail: SessionsChangedDetail,
): detail is SessionsChangedDetail & {
  event: "updated";
  session_name: string;
  state: Exclude<SessionActivityState, null>;
} {
  return detail.event === "updated" && typeof detail.session_name === "string" && isSessionActivityState(detail.state);
}

function isStructuralSessionRefresh(reason: string) {
  return !reason.startsWith("state-delta");
}

function handleDirtySessionRefresh(
  dirty: PendingSessionRefreshDirty,
  context: DirtySessionRefreshContext,
) {
  if (document.hidden) return;
  const minDelayMs = dirty.structural
    ? SESSION_STRUCTURAL_DIRTY_FOLLOWUP_MS
    : SESSION_STATE_DIRTY_FOLLOWUP_MS;
  const lastSuccessAt = context.lastSuccessfulRefreshAtRef.current;
  const elapsedMs = lastSuccessAt > 0 ? performance.now() - lastSuccessAt : 0;
  const delayMs = Math.max(minDelayMs - elapsedMs, 0);
  const followupReason = dirty.structural ? "dirty-followup" : "dirty-followup-state";
  if (delayMs > 0) {
    recordTerminalDiagnosticEvent("sessions_fetch_dirty_deferred", {
      reason: dirty.reason,
      requestId: context.requestId,
      followupReason,
      delayMs,
      structural: dirty.structural,
      activeSession: context.activeSessionRef.current,
      openSessions: context.openSessionsRef.current,
    });
    if (lastSuccessAt > 0) {
      recordTerminalDiagnosticEvent("sessions_fetch_dirty_skipped_recent", {
        reason: dirty.reason,
        requestId: context.requestId,
        elapsedMs,
        minDelayMs,
        structural: dirty.structural,
      });
    }
    if (!context.timerRef.current) {
      context.timerRef.current = setTimeout(() => {
        context.timerRef.current = null;
        context.fetchSessionsNow(followupReason);
      }, delayMs);
    }
    return;
  }
  recordTerminalDiagnosticEvent("sessions_fetch_dirty_followup", {
    reason: dirty.reason,
    requestId: context.requestId,
    structural: dirty.structural,
    activeSession: context.activeSessionRef.current,
    openSessions: context.openSessionsRef.current,
  });
  if (dirty.structural) {
    recordTerminalDiagnosticEvent("sessions_fetch_structural_followup", {
      reason: dirty.reason,
      requestId: context.requestId,
      activeSession: context.activeSessionRef.current,
      openSessions: context.openSessionsRef.current,
    });
  }
  context.fetchSessionsNow(followupReason);
}

function promoteHotSession(prev: string[], name: string) {
  const withoutCurrent = prev.filter((session) => session !== name);
  return [...withoutCurrent, name].slice(-MAX_HOT_TERMINALS_PER_BROWSER);
}

function deleteHotSession(prev: string[], name: string) {
  return prev.filter((session) => session !== name);
}

function renameHotSession(prev: string[], oldName: string, newName: string) {
  return prev.map((session) => (session === oldName ? newName : session));
}

function getHotSessionState(openSessions: string[], activeSession: string | null, name: string) {
  if (!openSessions.includes(name)) return "cold";
  if (activeSession === name) return "hot_active";
  return "hot_recent";
}

function getTerminalSessionKey(sessionName: string, session: Pick<Session, "session_uuid"> | null) {
  return session?.session_uuid ?? sessionName;
}

function getRestorePhaseFromLifecycle(event: TerminalWSLifecycleEvent): TerminalRestorePhase {
  switch (event.phase) {
    case "connect_started":
    case "direct_probe_completed":
    case "ticket_completed":
      return "preflight";
    case "preflight_completed":
    case "socket_created":
      return "websocket";
    case "socket_open":
      return "pty";
    case "socket_closed":
    case "reconnect_scheduled":
      return "retrying";
    case "preflight_failed":
    case "socket_error":
      return "error";
    default:
      return "opening";
  }
}

function getRestorePhaseFromStatus(status: WSConnectionStatus): TerminalRestorePhase {
  if (status === "connected") return "pty";
  if (status === "error") return "error";
  if (status === "disconnected") return "retrying";
  return "preflight";
}

function updateRestoreStateFromLifecycle(
  restore: TerminalRestoreState,
  event: TerminalWSLifecycleEvent,
): TerminalRestoreState {
  return {
    ...restore,
    phase: getRestorePhaseFromLifecycle(event),
    attempt: event.attempt,
    transport: event.transport ?? restore.transport ?? null,
    directProbeMs: event.phase === "direct_probe_completed" ? event.durationMs ?? null : restore.directProbeMs,
    ticketMs: event.phase === "ticket_completed" ? event.durationMs ?? null : restore.ticketMs,
    preflightMs: event.phase === "preflight_completed" ? event.elapsedMs ?? null : restore.preflightMs,
    socketOpenMs: event.phase === "socket_open" ? event.openWaitMs ?? null : restore.socketOpenMs,
    error: event.error ?? restore.error ?? null,
  };
}

function compactBytes(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)}M`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)}k`;
  return `${Math.round(value)}`;
}

function compactMs(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.round(value)}ms`;
}

function maxCounterP99(
  counters: TerminalCounterSummary[],
  name: TerminalCounterSummary["name"],
) {
  const values = counters
    .filter((counter) => counter.name === name && counter.p99 != null)
    .map((counter) => counter.p99 as number);
  return values.length ? Math.max(...values) : null;
}

function counterSum(
  counters: TerminalCounterSummary[],
  name: TerminalCounterSummary["name"],
  isActive: boolean,
) {
  const values = counters
    .filter((counter) => counter.name === name && counter.isActive === isActive)
    .map((counter) => counter.sum);
  return values.length ? values.reduce((total, value) => total + value, 0) : null;
}

function latencySummary(counters: TerminalCounterSummary[]) {
  const latencyCounters = counters.filter((counter) => counter.name === "send_recv_latency_ms");
  if (latencyCounters.length === 0) return "—";
  return latencyCounters
    .filter((counter) => counter.p99 != null)
    .sort((a, b) => (b.p99 ?? 0) - (a.p99 ?? 0))
    .slice(0, 3)
    .map((counter) => `${counter.kind ?? "any"} ${compactMs(counter.p99)}`)
    .join(" · ") || "—";
}

async function collectTerminalNetworkProbe(signal: AbortSignal, activeSession: string | null) {
  const networkStartedAt = performance.now();
  const probe = await getTerminalNetworkProbe({
    signal,
    bytes: 65_536,
  });
  const browserFetchMs = performance.now() - networkStartedAt;
  const sessionName = activeSession ?? undefined;
  recordCounterSample("api_probe_download_bytes", probe.payload_bytes, true, {
    sessionName,
  });
  recordCounterSample(
    "server_internet_probe_ms",
    probe.server_internet_probe.duration_ms,
    true,
    { sessionName },
  );
  recordTerminalDiagnosticEvent("terminal_network_probe", {
    activeSession,
    browserFetchMs,
    browserBytesPerSec: probe.payload_bytes / (browserFetchMs / 1000),
    serverInternetProbe: probe.server_internet_probe,
    payloadBytes: probe.payload_bytes,
  });
  return probe;
}

/**
 * Compact model label for the v2 footer strip. Matches SessionSidebar's
 * shortModel() behaviour but keeps a slightly longer slice (10 chars) so the
 * footer can show "Opus 4.7" instead of "opus".
 */
function shortenModelForFooter(model: string): string {
  const normalized = model
    .split("/")
    .at(-1)!
    .replace(/\[1m\]/g, "")
    .replace(/^(claude|gemini|gpt)-/, "");
  // Title-case "opus 4.7" / "sonnet 4.7" / "haiku 4"
  const titled = normalized
    .replace(/-/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
  return titled.length > 16 ? titled.slice(0, 16) + "…" : titled;
}

function hasOneMillionFooter(model: string): boolean {
  if (model.includes("[1m]")) return true;
  const normalized = model.split("/").at(-1) || model;
  return [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "gpt-5.5",
    "gpt-5.4",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
  ].some((prefix) => normalized.startsWith(prefix));
}

function hasRenderableMetrics(metrics: SessionMetrics): boolean {
  return (
    metrics.conversation_id != null ||
    metrics.context_pct_real != null ||
    metrics.input_tokens != null ||
    metrics.output_tokens != null ||
    metrics.reasoning_tokens != null
  );
}

function mergeSessionMetrics(session: Session, metrics: SessionMetrics): Session {
  const contextPct = metrics.context_pct_real ?? metrics.context_pct;
  return {
    ...session,
    conversation_id: metrics.conversation_id ?? session.conversation_id,
    model: metrics.model ?? session.model,
    last_context_pct: contextPct ?? session.last_context_pct,
    last_context_pct_real: contextPct ?? session.last_context_pct_real,
    last_context_pct_scaled:
      metrics.context_pct_scaled ?? session.last_context_pct_scaled,
    last_cost_usd: metrics.cost_usd ?? session.last_cost_usd,
    last_cost_conversation_usd:
      metrics.cost_conversation_usd ??
      metrics.cost_usd ??
      session.last_cost_conversation_usd,
    last_cost_session_usd:
      metrics.cost_session_usd ?? session.last_cost_session_usd,
    last_cost_session_incomplete: metrics.cost_session_incomplete,
    last_input_tokens: metrics.input_tokens ?? session.last_input_tokens,
    last_output_tokens: metrics.output_tokens ?? session.last_output_tokens,
    last_reasoning_tokens:
      metrics.reasoning_tokens ?? session.last_reasoning_tokens,
    working_seconds_msg:
      metrics.working_seconds_msg ?? session.working_seconds_msg,
    pricing_version: metrics.pricing_version ?? session.pricing_version,
    last_cost_conversation_equivalent_usd:
      metrics.cost_conversation_equivalent_usd ??
      session.last_cost_conversation_equivalent_usd,
    last_cost_session_equivalent_usd:
      metrics.cost_session_equivalent_usd ??
      session.last_cost_session_equivalent_usd,
    last_cost_equivalent_pricing_version:
      metrics.cost_equivalent_pricing_version ??
      session.last_cost_equivalent_pricing_version,
    metrics_refreshed_at: new Date().toISOString(),
  };
}

function preserveLocalSessionMetrics(incoming: Session, previous: Session | undefined): Session {
  if (!previous) return incoming;

  return {
    ...incoming,
    conversation_id: incoming.conversation_id ?? previous.conversation_id,
    model: incoming.model ?? previous.model,
    last_context_pct: incoming.last_context_pct ?? previous.last_context_pct,
    last_context_pct_real:
      incoming.last_context_pct_real ?? previous.last_context_pct_real,
    last_context_pct_scaled:
      incoming.last_context_pct_scaled ?? previous.last_context_pct_scaled,
    last_cost_usd: incoming.last_cost_usd ?? previous.last_cost_usd,
    last_cost_conversation_usd:
      incoming.last_cost_conversation_usd ??
      previous.last_cost_conversation_usd,
    last_cost_session_usd:
      incoming.last_cost_session_usd ?? previous.last_cost_session_usd,
    last_cost_session_incomplete:
      incoming.last_cost_session_incomplete ??
      previous.last_cost_session_incomplete,
    last_input_tokens: incoming.last_input_tokens ?? previous.last_input_tokens,
    last_output_tokens:
      incoming.last_output_tokens ?? previous.last_output_tokens,
    last_reasoning_tokens:
      incoming.last_reasoning_tokens ?? previous.last_reasoning_tokens,
    working_seconds_msg:
      incoming.working_seconds_msg ?? previous.working_seconds_msg,
    pricing_version: incoming.pricing_version ?? previous.pricing_version,
    last_cost_conversation_equivalent_usd:
      incoming.last_cost_conversation_equivalent_usd ??
      previous.last_cost_conversation_equivalent_usd,
    last_cost_session_equivalent_usd:
      incoming.last_cost_session_equivalent_usd ??
      previous.last_cost_session_equivalent_usd,
    last_cost_equivalent_pricing_version:
      incoming.last_cost_equivalent_pricing_version ??
      previous.last_cost_equivalent_pricing_version,
    metrics_refreshed_at:
      incoming.metrics_refreshed_at ?? previous.metrics_refreshed_at,
  };
}

interface TerminalPanelProps {
  panelVisible: boolean;
}

// eslint-disable-next-line sonarjs/cognitive-complexity -- Existing terminal orchestrator; Phase 1.1 keeps the UI wiring local to avoid a broad unrelated refactor.
export default function TerminalPanel({ panelVisible }: TerminalPanelProps) {
  const [openSessions, setOpenSessions] = useState<string[]>([]);
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [initialSessionHandled, setInitialSessionHandled] = useState(false);
  const [restoreStates, setRestoreStates] = useState<Record<string, TerminalRestoreState>>({});
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [allSessions, setAllSessions] = useState<Session[]>([]);
  const [pendingCommands, setPendingCommands] = useState<Record<string, string>>({});
  const [diagnosticsInfo, setDiagnosticsInfo] = useState(() => getTerminalDiagnosticsInfo());
  const [terminalMetricsEnabled, setTerminalMetricsEnabled] = useState(false);
  const [terminalMetrics, setTerminalMetrics] = useState<TerminalMetricsSnapshot | null>(null);
  const [terminalNetworkProbe, setTerminalNetworkProbe] =
    useState<TerminalNetworkProbeResponse | null>(null);
  const [terminalMetricsError, setTerminalMetricsError] = useState<string | null>(null);
  const [terminalNetworkError, setTerminalNetworkError] = useState<string | null>(null);
  const v2 = useDesignV2();
  const activeSessionMeta = activeSession
    ? allSessions.find((session) => session.name === activeSession) ?? null
    : null;

  // Keep panelVisible in a ref for event handlers
  const panelVisibleRef = useRef(panelVisible);
  panelVisibleRef.current = panelVisible;
  const activeSessionRef = useRef(activeSession);
  activeSessionRef.current = activeSession;
  const openSessionsRef = useRef(openSessions);
  openSessionsRef.current = openSessions;
  const allSessionsRef = useRef(allSessions);
  allSessionsRef.current = allSessions;
  const lastNetworkProbeAtRef = useRef(0);
  const telemetryUploadFailureCountRef = useRef(0);
  const telemetryNextUploadAttemptAtRef = useRef(0);
  const sessionRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sessionRefreshInFlightRef = useRef(false);
  const sessionRefreshDirtyRef = useRef<PendingSessionRefreshDirty | null>(null);
  const sessionRefreshDeferredHiddenRef = useRef(false);
  const sessionRefreshSequenceRef = useRef(0);
  const latestAppliedSessionRefreshRef = useRef(0);
  const lastSuccessfulSessionRefreshAtRef = useRef(0);
  const coldActivationStartedAtRef = useRef<Record<string, number>>({});
  const coldActivationTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const lastActiveSessionUrlKeyRef = useRef<string | null>(null);

  const syncDiagnosticsInfo = useCallback(() => {
    setDiagnosticsInfo((prev) => {
      const next = getTerminalDiagnosticsInfo();
      if (
        prev.active === next.active &&
        prev.startedAt === next.startedAt &&
        prev.expiresAt === next.expiresAt &&
        prev.remainingMs === next.remainingMs &&
        prev.eventCount === next.eventCount &&
        prev.droppedEventCount === next.droppedEventCount
      ) {
        return prev;
      }
      return next;
    });
  }, []);

  const cancelColdActivation = useCallback((sessionName: string, reason: string) => {
    const timer = coldActivationTimersRef.current[sessionName];
    if (timer != null) {
      clearTimeout(timer);
      delete coldActivationTimersRef.current[sessionName];
    }

    const startedAt = coldActivationStartedAtRef.current[sessionName];
    if (startedAt == null) return false;

    const sessionMeta = allSessionsRef.current.find((session) => session.name === sessionName) ?? null;
    delete coldActivationStartedAtRef.current[sessionName];
    setRestoreStates((prev) => {
      if (!prev[sessionName]) return prev;
      const next = { ...prev };
      delete next[sessionName];
      return next;
    });
    recordTerminalDiagnosticEvent("terminal_cold_to_hot_cancelled", {
      sessionName,
      sessionKey: getTerminalSessionKey(sessionName, sessionMeta),
      sessionUuid: sessionMeta?.session_uuid ?? null,
      durationMs: performance.now() - startedAt,
      reason,
    });
    return true;
  }, []);

  // Arm a watchdog so a cold->hot activation that never completes (PTY silent,
  // user switched away) is forced to "failed" instead of dangling as an
  // incomplete started event. Cleared on completion or cancellation.
  const armColdActivationTimeout = useCallback(
    (sessionName: string) => {
      const existing = coldActivationTimersRef.current[sessionName];
      if (existing != null) {
        clearTimeout(existing);
      }
      coldActivationTimersRef.current[sessionName] = setTimeout(() => {
        delete coldActivationTimersRef.current[sessionName];
        const startedAt = coldActivationStartedAtRef.current[sessionName];
        if (startedAt == null) return;
        const sessionMeta =
          allSessionsRef.current.find((session) => session.name === sessionName) ?? null;
        recordTerminalDiagnosticEvent("terminal_cold_to_hot_failed", {
          sessionName,
          sessionKey: getTerminalSessionKey(sessionName, sessionMeta),
          sessionUuid: sessionMeta?.session_uuid ?? null,
          reason: "timeout",
          durationMs: performance.now() - startedAt,
        });
        cancelColdActivation(sessionName, "timeout");
      }, COLD_TO_HOT_MAX_MS);
    },
    [cancelColdActivation]
  );

  const rememberSession = useCallback((session: Session) => {
    setAllSessions((prev) => {
      const withoutCurrent = prev.filter((item) => item.name !== session.name);
      return [...withoutCurrent, session];
    });
    recordTerminalDiagnosticEvent("session_metadata_remembered", {
      sessionName: session.name,
      provider: session.provider,
      sessionUuid: session.session_uuid,
      projectSlug: session.project_slug,
    });
  }, []);

  // Idempotency Map for session_renamed events (Plan 2026-05-21 AC11/AC13).
  const renameDedupRef = useRef<Map<string, number>>(new Map());

  const applySessionRenameDelta = useCallback((detail: SessionsChangedDetail) => {
    if (detail.event !== "renamed") return false;
    const oldName = detail.old_name;
    const newName = detail.new_name;
    const sessionInfo = detail.session_info;
    if (!oldName || !newName || !sessionInfo) {
      recordTerminalDiagnosticEvent("sessions_rename_delta_invalid", {
        detail,
      });
      return false;
    }

    // Dedup: drop event if same (old, new, updated_at) seen within TTL
    const dedupKey = `${oldName}|${newName}|${sessionInfo.updated_at}`;
    const dedupMap = renameDedupRef.current;
    const nowMs = Date.now();
    // GC: prune entries past TTL on every call (cheap, max 100 entries)
    for (const [key, ts] of dedupMap) {
      if (nowMs - ts > RENAME_DEDUP_TTL_MS) dedupMap.delete(key);
    }
    if (dedupMap.has(dedupKey)) {
      recordTerminalDiagnosticEvent("sessions_rename_delta_dedup", {
        oldName,
        newName,
      });
      return true;
    }
    // LRU evict if oversize (delete oldest)
    if (dedupMap.size >= RENAME_DEDUP_MAX_ENTRIES) {
      const oldestKey = dedupMap.keys().next().value;
      if (oldestKey) dedupMap.delete(oldestKey);
    }
    dedupMap.set(dedupKey, nowMs);

    // Optimistic patch allSessions in-place: replace entry with name === oldName
    // with the renamed entry. mergeStableSessions name-as-key trap is avoided
    // because we patch BEFORE the next poll merge.
    setAllSessions((prev) => {
      return prev.map((session) => {
        if (session.name !== oldName) return session;
        return {
          ...session,
          name: newName,
          display_name: sessionInfo.display_name ?? session.display_name,
          provider: sessionInfo.provider ?? session.provider,
          model: sessionInfo.model ?? session.model,
          project_slug: sessionInfo.project_slug ?? session.project_slug,
        };
      });
    });

    // Follow rename for active session pointer (AC4: route/state update)
    if (activeSessionRef.current === oldName) {
      setActiveSession(newName);
    }

    recordTerminalDiagnosticEvent("sessions_rename_delta_applied", {
      oldName,
      newName,
      activeSession: activeSessionRef.current,
    });
    return true;
  }, []);

  const applySessionStateDelta = useCallback((detail: SessionsChangedDetail) => {
    if (!isStateOnlySessionsChanged(detail)) return false;
    const sessionName = detail.session_name;
    const state = detail.state;
    const existing = allSessionsRef.current.find((session) => session.name === sessionName);
    if (!existing) {
      recordTerminalDiagnosticEvent("sessions_delta_missing_session", {
        sessionName,
        state,
        activeSession: activeSessionRef.current,
        openSessions: openSessionsRef.current,
      });
      return false;
    }
    setAllSessions((prev) => {
      return prev.map((session) => {
        if (session.name !== sessionName) return session;
        if (session.activity_state === state) return session;
        return { ...session, activity_state: state };
      });
    });
    recordTerminalDiagnosticEvent("sessions_delta_applied", {
      sessionName,
      state,
      activeSession: activeSessionRef.current,
      openSessions: openSessionsRef.current,
    });
    return true;
  }, []);

  const fetchSessionsNow = useCallback(async (reason: string) => {
    if (document.hidden) {
      sessionRefreshDeferredHiddenRef.current = true;
      recordTerminalDiagnosticEvent("sessions_fetch_deferred_hidden", {
        reason,
        activeSession: activeSessionRef.current,
        openSessions: openSessionsRef.current,
      });
      return;
    }

    if (sessionRefreshInFlightRef.current) {
      sessionRefreshDirtyRef.current = {
        reason,
        structural: isStructuralSessionRefresh(reason),
      };
      recordTerminalDiagnosticEvent("sessions_fetch_coalesced", {
        reason,
        state: "inflight",
        structural: isStructuralSessionRefresh(reason),
        activeSession: activeSessionRef.current,
        openSessions: openSessionsRef.current,
      });
      return;
    }

    sessionRefreshInFlightRef.current = true;
    const requestId = ++sessionRefreshSequenceRef.current;
    recordTerminalDiagnosticEvent("sessions_fetch_started", {
      reason,
      requestId,
      activeSession: activeSessionRef.current,
      openSessions: openSessionsRef.current,
    });

    try {
      const sessions = await listSessions();
      if (requestId < latestAppliedSessionRefreshRef.current) {
        recordTerminalDiagnosticEvent("sessions_fetch_outdated_ignored", {
          reason,
          requestId,
          latestAppliedRequestId: latestAppliedSessionRefreshRef.current,
        });
        return;
      }
      latestAppliedSessionRefreshRef.current = requestId;
      lastSuccessfulSessionRefreshAtRef.current = performance.now();
      setAllSessions((prev) => {
        const active = activeSessionRef.current;
        const hot = openSessionsRef.current;
        const fetchedNames = new Set(sessions.map((session) => session.name));
        const previousByName = new Map(prev.map((session) => [session.name, session]));
        const mergedSessions = sessions.map((session) =>
          preserveLocalSessionMetrics(session, previousByName.get(session.name)),
        );
        const preserved = prev.filter(
          (session) =>
            (session.name === active || hot.includes(session.name)) &&
            !fetchedNames.has(session.name),
        );
        return [...mergedSessions, ...preserved];
      });
      dispatchSessionsCountChanged(sessions.length);
      recordTerminalDiagnosticEvent("sessions_fetch_succeeded", {
        reason,
        requestId,
        count: sessions.length,
        activeSession: activeSessionRef.current,
        openSessions: openSessionsRef.current,
        providers: sessions.slice(0, 20).map((session) => ({ name: session.name, provider: session.provider })),
      });
    } catch (err) {
      sessionRefreshDirtyRef.current = {
        reason: "retry-after-failed-fetch",
        structural: true,
      };
      recordTerminalDiagnosticEvent("sessions_fetch_failed", {
        reason,
        requestId,
        activeSession: activeSessionRef.current,
        openSessions: openSessionsRef.current,
        error: err instanceof Error ? err.message : String(err),
      });
    } finally {
      sessionRefreshInFlightRef.current = false;
      const dirty = sessionRefreshDirtyRef.current;
      sessionRefreshDirtyRef.current = null;
      if (!dirty) return;
      handleDirtySessionRefresh(dirty, {
        requestId,
        timerRef: sessionRefreshTimerRef,
        lastSuccessfulRefreshAtRef: lastSuccessfulSessionRefreshAtRef,
        activeSessionRef,
        openSessionsRef,
        fetchSessionsNow,
      });
    }
  }, []);

  const scheduleSessionsRefresh = useCallback((reason: string) => {
    if (sessionRefreshTimerRef.current) {
      recordTerminalDiagnosticEvent("sessions_fetch_coalesced", {
        reason,
        state: "scheduled",
        activeSession: activeSessionRef.current,
        openSessions: openSessionsRef.current,
      });
      return;
    }
    sessionRefreshTimerRef.current = setTimeout(() => {
      sessionRefreshTimerRef.current = null;
      void fetchSessionsNow(reason);
    }, SESSION_REFRESH_COALESCE_MS);
  }, [fetchSessionsNow]);

  const refreshActiveSessionMetrics = useCallback(async (reason: string) => {
    const sessionName = activeSessionRef.current;
    if (!sessionName || !panelVisibleRef.current || document.hidden) return;

    try {
      const metrics = await getSessionMetrics(sessionName);
      if (!hasRenderableMetrics(metrics)) return;
      let updated = false;
      setAllSessions((prev) =>
        prev.map((session) => {
          if (session.name !== sessionName) return session;
          updated = true;
          return mergeSessionMetrics(session, metrics);
        }),
      );
      recordTerminalDiagnosticEvent("active_session_metrics_refreshed", {
        reason,
        sessionName,
        updated,
        conversationId: metrics.conversation_id,
        inputTokens: metrics.input_tokens ?? null,
        outputTokens: metrics.output_tokens ?? null,
      });
    } catch (err) {
      recordTerminalDiagnosticEvent("active_session_metrics_refresh_failed", {
        reason,
        sessionName,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }, []);

  useEffect(() => {
    if (!v2 || !activeSession) return;

    void refreshActiveSessionMetrics("active-session");
    const interval = window.setInterval(() => {
      void refreshActiveSessionMetrics("active-session-interval");
    }, ACTIVE_SESSION_METRICS_REFRESH_MS);

    function handleVisibilityChange() {
      if (!document.hidden) {
        void refreshActiveSessionMetrics("visibility-return");
      }
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [activeSession, activeSessionMeta?.name, refreshActiveSessionMetrics, v2]);

  // Fetch sessions for command palette + sidebar. Session-change bursts are
  // coalesced per browser so multiple terminal WS control messages do not
  // multiply into multiple `listSessions()` calls.
  useEffect(() => {
    scheduleSessionsRefresh("mount");

    function handleSessionsChanged(event: Event) {
      const detail = sessionsChangedDetail(event);
      // Plan 2026-05-21: handle dedicated "renamed" event with delta payload
      if (detail.event === "renamed") {
        if (applySessionRenameDelta(detail)) return;
        scheduleSessionsRefresh("rename-delta-missing-session");
        return;
      }
      if (isStateOnlySessionsChanged(detail)) {
        if (applySessionStateDelta(detail)) return;
        scheduleSessionsRefresh("state-delta-missing-session");
        return;
      }
      scheduleSessionsRefresh("sessions_changed");
    }
    function handleVisibilityChange() {
      if (!document.hidden && sessionRefreshDeferredHiddenRef.current) {
        sessionRefreshDeferredHiddenRef.current = false;
        scheduleSessionsRefresh("visibility-return");
      }
    }
    // Post WS reconnect: catch up on events missed while disconnected (AC6).
    function handleWsReconnect() {
      scheduleSessionsRefresh("ws-reconnect");
    }
    window.addEventListener("marvisx:sessions_changed", handleSessionsChanged);
    window.addEventListener("marvisx:ws_reconnect", handleWsReconnect);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      if (sessionRefreshTimerRef.current) {
        clearTimeout(sessionRefreshTimerRef.current);
        sessionRefreshTimerRef.current = null;
      }
      window.removeEventListener("marvisx:sessions_changed", handleSessionsChanged);
      window.removeEventListener("marvisx:ws_reconnect", handleWsReconnect);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [applySessionStateDelta, applySessionRenameDelta, scheduleSessionsRefresh]);

  useEffect(() => {
    bootTerminalDiagnosticsFromLocation();
    registerTerminalDiagnosticsConsole();
    syncDiagnosticsInfo();

    const refresh = () => syncDiagnosticsInfo();
    const changeEventName = getTerminalDiagnosticsChangeEventName();
    window.addEventListener(changeEventName, refresh);
    window.addEventListener("storage", refresh);

    return () => {
      window.removeEventListener(changeEventName, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, [syncDiagnosticsInfo]);

  // Countdown ticker — only while diagnostics is active. When inactive the
  // info is stable (active:false, remainingMs:0) so a 1Hz re-render is waste.
  useEffect(() => {
    if (!diagnosticsInfo.active) return;
    const ticker = setInterval(syncDiagnosticsInfo, 1000);
    return () => clearInterval(ticker);
  }, [diagnosticsInfo.active, syncDiagnosticsInfo]);

  useEffect(() => {
    recordTerminalDiagnosticEvent("terminal_panel_mount", {
      panelVisible,
      route: window.location.pathname,
    });

    const handleVisibility = () => {
      recordTerminalDiagnosticEvent("browser_visibility_change", {
        hidden: document.hidden,
        activeSession: activeSessionRef.current,
      });
    };
    const handleFocus = () => recordTerminalDiagnosticEvent("browser_focus", { activeSession: activeSessionRef.current });
    const handleBlur = () => recordTerminalDiagnosticEvent("browser_blur", { activeSession: activeSessionRef.current });
    const handlePageShow = () => recordTerminalDiagnosticEvent("browser_pageshow", { activeSession: activeSessionRef.current });
    const handlePageHide = () => recordTerminalDiagnosticEvent("browser_pagehide", { activeSession: activeSessionRef.current });

    document.addEventListener("visibilitychange", handleVisibility);
    window.addEventListener("focus", handleFocus);
    window.addEventListener("blur", handleBlur);
    window.addEventListener("pageshow", handlePageShow);
    window.addEventListener("pagehide", handlePageHide);
    return () => {
      recordTerminalDiagnosticEvent("terminal_panel_unmount", { activeSession: activeSessionRef.current });
      document.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("focus", handleFocus);
      window.removeEventListener("blur", handleBlur);
      window.removeEventListener("pageshow", handlePageShow);
      window.removeEventListener("pagehide", handlePageHide);
      // Sweep any in-flight cold->hot watchdog timers so they cannot fire after unmount.
      for (const timer of Object.values(coldActivationTimersRef.current)) {
        clearTimeout(timer);
      }
      coldActivationTimersRef.current = {};
    };
  }, []);

  // Cmd+J / Ctrl+J keybinding for session switcher — gated on panelVisible
  // (Cmd+K is reserved for GlobalSearch)
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (!panelVisibleRef.current) return;
      if ((e.metaKey || e.ctrlKey) && e.key === "j") {
        e.preventDefault();
        setPaletteOpen((prev) => !prev);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const handleSelectSession = useCallback(
    (name: string, reason = "manual", sessionOverride: Session | null = null) => {
      const previousOpenSessions = openSessions;
      const previousState = getHotSessionState(previousOpenSessions, activeSession, name);
      const sessionMeta = sessionOverride ?? allSessionsRef.current.find((session) => session.name === name) ?? null;
      const sessionKey = getTerminalSessionKey(name, sessionMeta);
      recordTerminalDiagnosticEvent("active_session_select", {
        reason,
        previousSession: activeSession,
        nextSession: name,
        openSessions: previousOpenSessions,
        previousState,
        sessionKey,
      });
      if (previousState === "cold") {
        const startedAt = performance.now();
        coldActivationStartedAtRef.current[name] = startedAt;
        armColdActivationTimeout(name);
        setRestoreStates((prev) => ({
          ...prev,
          [name]: {
            sessionName: name,
            startedAt,
            phase: "opening",
            status: "connecting",
            attempt: 0,
          },
        }));
        recordTerminalDiagnosticEvent("terminal_cold_to_hot_started", {
          sessionName: name,
          sessionKey,
          sessionUuid: sessionMeta?.session_uuid ?? null,
          previousState,
          reason,
          hotCount: previousOpenSessions.length,
          coldCount: Math.max(allSessionsRef.current.length - previousOpenSessions.length, 0),
          restoreUi: "phase-loader",
        });
      } else {
        setRestoreStates((prev) => {
          if (!prev[name]) return prev;
          const next = { ...prev };
          delete next[name];
          return next;
        });
      }
      const nextHotSessions = promoteHotSession(previousOpenSessions, name);
      for (const sessionName of previousOpenSessions.filter((session) => !nextHotSessions.includes(session))) {
        cancelColdActivation(sessionName, "demoted-before-pty-ready");
        recordTerminalDiagnosticEvent("terminal_hot_session_demoted", {
          sessionName,
          reason,
          nextHotSessions,
        });
      }
      recordTerminalDiagnosticEvent("terminal_hot_session_promoted", {
        sessionName: name,
        reason,
        previousState,
        nextHotSessions,
      });
      setOpenSessions(nextHotSessions);
      setActiveSession(name);
    },
    [activeSession, armColdActivationTimeout, cancelColdActivation, openSessions]
  );

  // Update URL and save last session when active session changes — gated on panelVisible
  useEffect(() => {
    if (!activeSession) return;
    if (!panelVisible) return;
    const session = allSessions.find((s) => s.name === activeSession);
    const uuid = session?.session_uuid ?? null;
    const newPath = uuid
      ? `/terminal/${uuid}`
      : `/terminal/${encodeURIComponent(activeSession)}`;
    const urlKey = `${activeSession}|${uuid ?? ""}|${newPath}`;
    if (lastActiveSessionUrlKeyRef.current === urlKey) return;
    lastActiveSessionUrlKeyRef.current = urlKey;
    window.history.replaceState({}, "", newPath);
    saveLastSession(uuid, activeSession);
    recordTerminalDiagnosticEvent("active_session_url_updated", {
      activeSession,
      sessionUuid: uuid,
      route: newPath,
      provider: session?.provider ?? null,
    });
  }, [activeSession, allSessions, panelVisible]);

  const resetToTerminalRoot = useCallback((reason: string, blockedName?: string) => {
    if (blockedName) clearLastSession();
    lastActiveSessionUrlKeyRef.current = null;
    window.history.replaceState({}, "", "/terminal/");
    recordTerminalDiagnosticEvent("route_terminal_root_restored", {
      reason,
      blockedName: blockedName ?? null,
    });
  }, []);

  const restoreLastSession = useCallback(
    async (reason: string, blockedName?: string) => {
      const last = loadLastSession();
      if (!last || last.name === blockedName) {
        resetToTerminalRoot(reason, blockedName);
        return;
      }

      if (last.uuid) {
        try {
          const session = await getSessionByUUID(last.uuid);
          rememberSession(session);
          handleSelectSession(session.name, reason, session);
          return;
        } catch {
          recordTerminalDiagnosticEvent("restore_uuid_resolution_failed", {
            reason,
            sessionUuid: last.uuid,
            sessionName: last.name,
          });
        }
      }

      try {
        const sessions = await listSessions();
        const session = sessions.find((item) => item.name === last.name);
        if (!session) {
          resetToTerminalRoot(`${reason}-name-missing`, last.name);
          return;
        }
        rememberSession(session);
        handleSelectSession(session.name, reason, session);
      } catch {
        resetToTerminalRoot(`${reason}-session-list-failed`, last.name);
      }
    },
    [handleSelectSession, rememberSession, resetToTerminalRoot]
  );

  const selectRouteUuid = useCallback(
    async (targetUuid: string) => {
      try {
        const session = await getSessionByUUID(targetUuid);
        rememberSession(session);
        recordTerminalDiagnosticEvent("route_uuid_resolved", {
          targetUuid,
          sessionName: session.name,
          provider: session.provider,
        });
        handleSelectSession(session.name, "route-uuid", session);
      } catch {
        recordTerminalDiagnosticEvent("route_uuid_resolution_failed", { targetUuid });
        await restoreLastSession("restore-after-missing-route-uuid");
      }
    },
    [handleSelectSession, rememberSession, restoreLastSession]
  );

  const selectRouteSessionName = useCallback(
    async (targetName: string, reason = "path-session") => {
      try {
        const sessions = await listSessions();
        const session = sessions.find((item) => item.name === targetName);
        if (!session) {
          recordTerminalDiagnosticEvent("route_session_name_not_found", {
            target: targetName,
            sessionCount: sessions.length,
          });
          await restoreLastSession("restore-after-missing-route-name", targetName);
          return;
        }
        rememberSession(session);
        handleSelectSession(session.name, reason, session);
      } catch {
        recordTerminalDiagnosticEvent("route_session_name_check_failed", { target: targetName });
        await restoreLastSession("restore-after-route-name-check-failed", targetName);
      }
    },
    [handleSelectSession, rememberSession, restoreLastSession]
  );

  // Handle ?session= or ?uuid= query param, pathname UUID, and last-session restore
  useEffect(() => {
    if (!panelVisible) return;
    if (initialSessionHandled) return;

    const params = new URLSearchParams(window.location.search);
    const sessionParam = params.get("session");
    if (sessionParam) {
      recordTerminalDiagnosticEvent("route_session_param_detected", { sessionName: sessionParam });
      setInitialSessionHandled(true);
      void selectRouteSessionName(sessionParam, "query-session");
      return;
    }

    const uuidParam = params.get("uuid");

    // Check pathname for UUID or session name (e.g. /terminal/some-uuid or /terminal/ci-v4-pdf)
    const pathSegments = window.location.pathname.split("/").filter(Boolean);
    const pathSlug = pathSegments.length >= 2 ? decodeURIComponent(pathSegments[pathSegments.length - 1]) : null;

    const targetUuid = uuidParam || pathSlug;
    const isUUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

    if (targetUuid) {
      setInitialSessionHandled(true);
      recordTerminalDiagnosticEvent("route_target_detected", {
        target: targetUuid,
        isUuid: isUUID.test(targetUuid),
      });

      if (isUUID.test(targetUuid)) {
        void selectRouteUuid(targetUuid);
      } else {
        void selectRouteSessionName(targetUuid);
      }
      return;
    }

    // No URL params -> restore last session from localStorage
    setInitialSessionHandled(true);
    void restoreLastSession("restore-last-session");
  }, [
    handleSelectSession,
    initialSessionHandled,
    panelVisible,
    restoreLastSession,
    selectRouteSessionName,
    selectRouteUuid,
  ]);

  const handleSessionCreated = useCallback((name: string, initialCommand?: string) => {
    const startedAt = performance.now();
    coldActivationStartedAtRef.current[name] = startedAt;
    armColdActivationTimeout(name);
    setRestoreStates((prev) => ({
      ...prev,
      [name]: {
        sessionName: name,
        startedAt,
        phase: "opening",
        status: "connecting",
        attempt: 0,
      },
    }));
    setOpenSessions((prev) => promoteHotSession(prev, name));
    setActiveSession(name);
    recordTerminalDiagnosticEvent("session_created", {
      sessionName: name,
      hasInitialCommand: Boolean(initialCommand),
    });
    if (initialCommand) {
      setPendingCommands((prev) => ({ ...prev, [name]: initialCommand }));
    }
  }, [armColdActivationTimeout]);

  const handleSessionDeleted = useCallback(
    (name: string) => {
      cancelColdActivation(name, "deleted-before-pty-ready");
      setOpenSessions((prev) => deleteHotSession(prev, name));
      recordTerminalDiagnosticEvent("session_deleted", {
        sessionName: name,
        activeSession,
      });
      if (activeSession === name) {
        const remaining = openSessions.filter((s) => s !== name);
        setActiveSession(
          remaining.length > 0 ? remaining[remaining.length - 1] : null
        );
      }
    },
    [activeSession, cancelColdActivation, openSessions]
  );

  const handleSessionRenamed = useCallback(
    (oldName: string, newName: string) => {
      setOpenSessions((prev) => renameHotSession(prev, oldName, newName));
      recordTerminalDiagnosticEvent("session_renamed", {
        oldName,
        newName,
        activeSession,
      });
      if (activeSession === oldName) {
        setActiveSession(newName);
      }
      setPendingCommands((prev) => {
        if (!(oldName in prev)) return prev;
        const next = { ...prev, [newName]: prev[oldName] };
        delete next[oldName];
        return next;
      });
      if (coldActivationStartedAtRef.current[oldName] != null) {
        coldActivationStartedAtRef.current[newName] = coldActivationStartedAtRef.current[oldName];
        delete coldActivationStartedAtRef.current[oldName];
        // Re-key the cold->hot watchdog under the new name so it keeps tracking
        // the same in-flight activation (timer callbacks close over the name).
        const oldTimer = coldActivationTimersRef.current[oldName];
        if (oldTimer != null) {
          clearTimeout(oldTimer);
          delete coldActivationTimersRef.current[oldName];
        }
        armColdActivationTimeout(newName);
      }
      setRestoreStates((prev) => {
        const restore = prev[oldName];
        if (!restore) return prev;
        const next = { ...prev };
        delete next[oldName];
        next[newName] = { ...restore, sessionName: newName };
        return next;
      });
    },
    [activeSession, armColdActivationTimeout]
  );

  const handleStatusChange = useCallback(
    (session: string, wsStatus: WSConnectionStatus) => {
      setRestoreStates((prev) => {
        const restore = prev[session];
        if (!restore) return prev;
        return {
          ...prev,
          [session]: {
            ...restore,
            phase: getRestorePhaseFromStatus(wsStatus),
            status: wsStatus,
          },
        };
      });
      const sessionMeta = allSessionsRef.current.find((item) => item.name === session) ?? null;
      const sessionKey = getTerminalSessionKey(session, sessionMeta);
      if (wsStatus === "connected" && coldActivationStartedAtRef.current[session] != null) {
        const startedAt = coldActivationStartedAtRef.current[session];
        recordTerminalDiagnosticEvent("terminal_cold_ws_connected", {
          sessionName: session,
          sessionKey,
          sessionUuid: sessionMeta?.session_uuid ?? null,
          durationMs: performance.now() - startedAt,
        });
      }
      if (wsStatus === "error" && coldActivationStartedAtRef.current[session] != null) {
        const startedAt = coldActivationStartedAtRef.current[session];
        // WS error already resolves this activation as failed; disarm the
        // watchdog so it does not later emit a duplicate timeout failure.
        const errorTimer = coldActivationTimersRef.current[session];
        if (errorTimer != null) {
          clearTimeout(errorTimer);
          delete coldActivationTimersRef.current[session];
        }
        recordTerminalDiagnosticEvent("terminal_cold_to_hot_failed", {
          sessionName: session,
          sessionKey,
          sessionUuid: sessionMeta?.session_uuid ?? null,
          durationMs: performance.now() - startedAt,
          status: wsStatus,
        });
      }
    },
    []
  );

  const handleLifecycleEvent = useCallback((session: string, event: TerminalWSLifecycleEvent) => {
    setRestoreStates((prev) => {
      const restore = prev[session];
      if (!restore) return prev;
      return {
        ...prev,
        [session]: updateRestoreStateFromLifecycle(restore, event),
      };
    });
  }, []);

  const handlePtyOutputParsed = useCallback((session: string) => {
    const sessionMeta = allSessionsRef.current.find((item) => item.name === session) ?? null;
    const sessionKey = getTerminalSessionKey(session, sessionMeta);
    setRestoreStates((prev) => {
      if (!prev[session]) return prev;
      const next = { ...prev };
      delete next[session];
      return next;
    });

    const startedAt = coldActivationStartedAtRef.current[session];
    if (startedAt == null) return;

    const durationMs = performance.now() - startedAt;
    delete coldActivationStartedAtRef.current[session];
    const completionTimer = coldActivationTimersRef.current[session];
    if (completionTimer != null) {
      clearTimeout(completionTimer);
      delete coldActivationTimersRef.current[session];
    }
    recordTerminalDiagnosticEvent("terminal_cold_pty_ready", {
      sessionName: session,
      sessionKey,
      sessionUuid: sessionMeta?.session_uuid ?? null,
      durationMs,
    });
    recordTerminalDiagnosticEvent("terminal_cold_to_hot_completed", {
      sessionName: session,
      sessionKey,
      sessionUuid: sessionMeta?.session_uuid ?? null,
      durationMs,
      hotCount: openSessionsRef.current.length,
      coldCount: Math.max(allSessionsRef.current.length - openSessionsRef.current.length, 0),
    });
  }, []);

  // Stable callback refs for Terminal memo — avoids inline closures that break React.memo
  const statusCallbacksRef = useRef<Record<string, (s: WSConnectionStatus) => void>>({});
  const getStatusCallback = useCallback((name: string) => {
    if (!statusCallbacksRef.current[name]) {
      statusCallbacksRef.current[name] = (s: WSConnectionStatus) => {
        handleStatusChange(name, s);
      };
    }
    return statusCallbacksRef.current[name];
  }, [handleStatusChange]);

  const lifecycleCallbacksRef = useRef<Record<string, (event: TerminalWSLifecycleEvent) => void>>({});
  const getLifecycleCallback = useCallback((name: string) => {
    if (!lifecycleCallbacksRef.current[name]) {
      lifecycleCallbacksRef.current[name] = (event: TerminalWSLifecycleEvent) => {
        handleLifecycleEvent(name, event);
      };
    }
    return lifecycleCallbacksRef.current[name];
  }, [handleLifecycleEvent]);

  const ptyOutputParsedCallbacksRef = useRef<Record<string, () => void>>({});
  const getPtyOutputParsedCallback = useCallback((name: string) => {
    if (!ptyOutputParsedCallbacksRef.current[name]) {
      ptyOutputParsedCallbacksRef.current[name] = () => {
        handlePtyOutputParsed(name);
      };
    }
    return ptyOutputParsedCallbacksRef.current[name];
  }, [handlePtyOutputParsed]);

  const commandSentCallbacksRef = useRef<Record<string, () => void>>({});
  const getCommandSentCallback = useCallback((name: string) => {
    if (!commandSentCallbacksRef.current[name]) {
      commandSentCallbacksRef.current[name] = () => {
        setPendingCommands((prev) => {
          const next = { ...prev };
          delete next[name];
          return next;
        });
      };
    }
    return commandSentCallbacksRef.current[name];
  }, []);

  const handleAuthError = useCallback(() => {
    recordTerminalDiagnosticEvent("terminal_auth_error_redirect", {
      activeSession,
    });
    redirectToConsoleLogin();
  }, [activeSession]);

  const sessionMetaByName = new Map(allSessions.map((session) => [session.name, session]));
  const activeRestoreState = activeSession ? restoreStates[activeSession] ?? null : null;

  useEffect(() => {
    const hotCount = openSessions.length;
    const coldCount = Math.max(allSessions.length - hotCount, 0);
    const processSummary = summarizeSessionProcess(allSessions);
    recordCounterSample("mounted_terminal_count", hotCount, true, {
      sessionName: activeSession ?? undefined,
    });
    recordCounterSample("hot_terminal_count", hotCount, true, {
      sessionName: activeSession ?? undefined,
    });
    recordCounterSample("cold_terminal_count", coldCount, true, {
      sessionName: activeSession ?? undefined,
    });
    recordTerminalDiagnosticEvent("terminal_hot_cold_counts", {
      activeSession,
      hotSessions: openSessions,
      totalSessionCount: allSessions.length,
      hotCount,
      coldCount,
      processSummary,
    });
    recordTerminalDiagnosticEvent("terminal_panel_state", {
      activeSession,
      provider: activeSessionMeta?.provider ?? null,
      sessionUuid: activeSessionMeta?.session_uuid ?? null,
      openSessions,
      panelVisible,
      processSummary,
    });
  }, [activeSession, activeSessionMeta?.provider, activeSessionMeta?.session_uuid, allSessions, openSessions, panelVisible]);

  const toggleDiagnostics = useCallback(() => {
    if (diagnosticsInfo.active) {
      stopTerminalDiagnostics("toolbar-stop");
    } else {
      startTerminalDiagnostics("toolbar-start");
      recordTerminalDiagnosticEvent("diagnostics_toolbar_started_with_context", {
        activeSession,
        provider: activeSessionMeta?.provider ?? null,
        sessionUuid: activeSessionMeta?.session_uuid ?? null,
        openSessions,
        panelVisible,
      });
    }
    syncDiagnosticsInfo();
  }, [activeSession, activeSessionMeta?.provider, activeSessionMeta?.session_uuid, diagnosticsInfo.active, openSessions, panelVisible, syncDiagnosticsInfo]);

  const diagnosticsActive = diagnosticsInfo.active;
  const terminalMetricsPollingEnabled = terminalMetricsEnabled || diagnosticsActive;

  useEffect(() => {
    if (!terminalMetricsPollingEnabled || !panelVisible) return;
    const controller = new AbortController();
    let cancelled = false;

    async function fetchNetworkProbeIfDue() {
      const now = Date.now();
      if (now - lastNetworkProbeAtRef.current < 30_000) return;
      lastNetworkProbeAtRef.current = now;
      try {
        const probe = await collectTerminalNetworkProbe(
          controller.signal,
          activeSessionRef.current,
        );
        if (cancelled) return;
        setTerminalNetworkProbe(probe);
        setTerminalNetworkError(null);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : String(err);
        const errorKind = classifyTerminalTelemetryError(err);
        setTerminalNetworkError(message);
        recordTerminalDiagnosticEvent("terminal_network_probe_failed", {
          activeSession: activeSessionRef.current,
          error: message,
          errorKind,
          hidden: document.hidden,
        });
      }
    }

    async function fetchMetrics() {
      try {
        const fetchStartedAt = performance.now();
        const snapshot = await getTerminalMetrics({ signal: controller.signal });
        const apiFetchRttMs = performance.now() - fetchStartedAt;
        if (cancelled) return;
        setTerminalMetrics(snapshot);
        setTerminalMetricsError(null);
        recordCounterSample("api_fetch_rtt_ms", apiFetchRttMs, true, {
          sessionName: activeSessionRef.current ?? undefined,
        });
        recordTerminalDiagnosticEvent("terminal_metrics_fetched", {
          activeSession: activeSessionRef.current,
          apiFetchRttMs,
          pollSource: terminalMetricsEnabled ? "perf" : "diagnostics",
          hidden: document.hidden,
          liveWebsocketCount: snapshot.live_websocket_count,
          livePtyReaderCount: snapshot.live_pty_reader_count,
          sessionCount: Object.keys(snapshot.sessions).length,
          snapshot,
        });
        await fetchNetworkProbeIfDue();
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : String(err);
        const errorKind = classifyTerminalTelemetryError(err);
        setTerminalMetricsError(message);
        recordTerminalDiagnosticEvent("terminal_metrics_fetch_failed", {
          activeSession: activeSessionRef.current,
          error: message,
          errorKind,
          pollSource: terminalMetricsEnabled ? "perf" : "diagnostics",
          hidden: document.hidden,
        });
      }
    }

    fetchMetrics();
    const interval = window.setInterval(fetchMetrics, 10_000);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(interval);
    };
  }, [panelVisible, terminalMetricsEnabled, terminalMetricsPollingEnabled]);

  useEffect(() => {
    if (!diagnosticsActive) return;
    const controller = new AbortController();
    let cancelled = false;

    async function postBatch() {
      const now = Date.now();
      if (now < telemetryNextUploadAttemptAtRef.current) return;
      const batch = getPendingTerminalDiagnosticsBatch(750);
      if (!batch || batch.events.length === 0) return;
      try {
        await postTerminalMetricsBatch(batch, { signal: controller.signal });
        if (cancelled) return;
        telemetryUploadFailureCountRef.current = 0;
        telemetryNextUploadAttemptAtRef.current = 0;
        markTerminalDiagnosticsBatchPosted(batch.events.map((event) => event.id));
        recordTerminalDiagnosticEvent("terminal_metrics_batch_posted", {
          runId: batch.run_id,
          eventCount: batch.events.length,
          counterCount: batch.counters.length,
          pendingEventCount: Math.max(0, batch.telemetry_health.pendingEvents - batch.events.length),
          hidden: document.hidden,
        });
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        telemetryUploadFailureCountRef.current += 1;
        const failureCount = telemetryUploadFailureCountRef.current;
        const nextRetryMs = Math.min(60_000, 5_000 * (2 ** Math.min(failureCount - 1, 4)));
        telemetryNextUploadAttemptAtRef.current = Date.now() + nextRetryMs;
        recordTerminalDiagnosticEvent("terminal_metrics_batch_post_failed", {
          error: err instanceof Error ? err.message : String(err),
          errorKind: classifyTerminalTelemetryError(err),
          failureCount,
          nextRetryMs,
          pendingEventCount: batch.events.length,
          hidden: document.hidden,
        });
      }
    }

    postBatch();
    const interval = window.setInterval(postBatch, 5_000);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(interval);
    };
  }, [diagnosticsActive]);

  const markCurrentIssue = useCallback(() => {
    const note = window.prompt("Cosa vedi in questo momento?");
    if (note === null) return;
    markTerminalDiagnostic(note, {
      activeSession,
      provider: activeSessionMeta?.provider ?? null,
      sessionUuid: activeSessionMeta?.session_uuid ?? null,
      openSessions,
      panelVisible,
    });
    syncDiagnosticsInfo();
  }, [activeSession, activeSessionMeta?.provider, activeSessionMeta?.session_uuid, openSessions, panelVisible, syncDiagnosticsInfo]);

  const diagnosticsMinutesLeft = diagnosticsInfo.active
    ? Math.max(1, Math.ceil(diagnosticsInfo.remainingMs / 60000))
    : 30;
  const diagnosticCounters = diagnosticsInfo.counters ?? [];
  const activeServerMetrics = activeSession ? terminalMetrics?.sessions[activeSession] ?? null : null;
  const rxActive = counterSum(diagnosticCounters, "bytes_received_per_sec", true);
  const rxHidden = counterSum(diagnosticCounters, "bytes_received_per_sec", false);
  const parseP99 = maxCounterP99(diagnosticCounters, "parse_ms");
  const wheelEventsPreCoalesce = counterSum(diagnosticCounters, "wheel_events_pre_coalesce", true);
  const wheelEventsPostCoalesce =
    counterSum(diagnosticCounters, "wheel_events_post_coalesce", true) ??
    counterSum(diagnosticCounters, "wheel_events_per_sec", true);
  const wheelText =
    wheelEventsPreCoalesce == null && wheelEventsPostCoalesce == null
      ? "—"
      : `${Math.round(wheelEventsPostCoalesce ?? 0)}/${Math.round(wheelEventsPreCoalesce ?? wheelEventsPostCoalesce ?? 0)}`;
  const latencyText = latencySummary(diagnosticCounters);
  const apiRttP99 = maxCounterP99(diagnosticCounters, "api_fetch_rtt_ms");
  const serverInternetP99 =
    terminalMetrics?.network?.internet_probe_duration_ms.p99 ??
    maxCounterP99(diagnosticCounters, "server_internet_probe_ms");
  const websocketRttP99 =
    activeServerMetrics?.websocket_ping_rtt_ms?.p99 ??
    terminalMetrics?.network?.websocket_ping_rtt_ms.p99;
  const eventLoopLagP99 = terminalMetrics?.network?.event_loop_lag_ms.p99;
  const processRssMb =
    terminalMetrics?.process?.rss_bytes != null
      ? terminalMetrics.process.rss_bytes / (1024 * 1024)
      : null;
  const latestNetworkProbeStatus = terminalNetworkProbe?.server_internet_probe.ok
    ? terminalNetworkProbe.server_internet_probe.status_code ?? "ok"
    : "fail";
  const latestNetworkStatus = terminalNetworkProbe
    ? `${terminalNetworkProbe.server_internet_probe.target}:${latestNetworkProbeStatus}`
    : null;

  // Footer strip metrics derived from active session. PR2 wires the dual
  // cost + real/scaled ctx + input/output tokens (previously "–" placeholders).
  const ctxReal =
    activeSessionMeta?.last_context_pct_real ??
    activeSessionMeta?.last_context_pct ??
    null;
  const ctxScaled = activeSessionMeta?.last_context_pct_scaled ?? null;
  const costConv =
    activeSessionMeta?.last_cost_conversation_usd ??
    activeSessionMeta?.last_cost_usd ??
    null;
  const costSession = activeSessionMeta?.last_cost_session_usd ?? null;
  // PR4: shadow "cost_equivalent" — what the session WOULD cost at API rates
  // when cost is free (OAuth). Null when unknown (fallback_strategy=skip).
  const costEquivalent =
    activeSessionMeta?.last_cost_session_equivalent_usd ??
    activeSessionMeta?.last_cost_conversation_equivalent_usd ??
    null;
  const inTokens = activeSessionMeta?.last_input_tokens ?? null;
  const outTokens = activeSessionMeta?.last_output_tokens ?? null;
  const modelName = activeSessionMeta?.model ?? null;
  const shellsCount = openSessions.length;
  const parkedSessionsCount = Math.max(allSessions.length - openSessions.length, 0);
  const metricsRefreshedAt = activeSessionMeta?.metrics_refreshed_at ?? null;
  const metricsStale = metricsRefreshedAt
    ? Date.now() - Date.parse(metricsRefreshedAt) > 3_600_000
    : false;

  // Breakpoint detection for responsive truncation (PR2). Thresholds:
  //   >1200: full (labels + 1M badge + dual cost + dual ctx)
  //   900-1200: drop 1M + cost_session
  //   600-900: drop labels
  //   <600: drop scaled ctx + verbose out + minutes
  const [footerWidth, setFooterWidth] = useState<number>(1200);
  useEffect(() => {
    if (!v2) return;
    function update() {
      setFooterWidth(window.innerWidth);
    }
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [v2]);

  const [clockNow, setClockNow] = useState<string>("");
  useEffect(() => {
    if (!v2) return;
    function tick() {
      const d = new Date();
      setClockNow(
        `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`,
      );
    }
    tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, [v2]);

  // PR2 formatters inline (avoid cross-file import for tiny helpers)
  const fmtTokens = (n: number | null): string => {
    if (n == null) return "—";
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 100_000) return `${Math.round(n / 1000)}k`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return `${n}`;
  };
  const fmtCostDual = (conv: number | null, session: number | null): string => {
    if (conv == null && session == null) return "—";
    if (conv == null) return session == null ? "—" : `$${session.toFixed(1)}`;
    if (session == null) return `$${conv.toFixed(2)}`;
    if (Math.abs(conv - session) < 0.01) return `$${conv.toFixed(2)}`;
    return `$${conv.toFixed(1)}/${session.toFixed(1)}`;
  };
  const fmtCtxDual = (real: number | null, scaled: number | null): string => {
    if (real == null && scaled == null) return "—";
    const r = real != null ? `${Math.round(real)}%` : "—";
    const s = scaled != null ? `${Math.round(scaled)}%` : "—";
    if (r === s) return r;
    return `${r}/${s}`;
  };

  // Backwards-compat value for the metric bar placeholder — real ctx% drives
  // the width fill; scaled + conv/session are rendered as text alongside.
  const ctxPct = ctxReal;

  // Responsive truncation cascade (PR2). Computed once per render so the JSX
  // stays a flat tree of conditionals (keeps cognitive complexity low).
  const showLabels = footerWidth >= 600;
  const showOneMBadge = footerWidth >= 1200;
  const showCostSession = footerWidth >= 1200;
  const showScaledCtx = footerWidth >= 600;
  const showOutValue = footerWidth >= 600;

  let ctxText: string;
  if (showScaledCtx) {
    ctxText = fmtCtxDual(ctxReal, ctxScaled);
  } else if (ctxReal != null) {
    ctxText = `${Math.round(ctxReal)}%`;
  } else {
    ctxText = "—";
  }

  let costText: string;
  if (showCostSession) {
    costText = fmtCostDual(costConv, costSession);
  } else if (costConv != null) {
    costText = `$${costConv.toFixed(2)}`;
  } else {
    costText = "—";
  }

  // PR4: inline shadow cost "(est $X.XX)" appended when equivalent exceeds
  // real (OAuth/free sessions). Null when equivalent missing or matches real
  // within 1¢ (Claude sessions).
  const costRealForBadge = costSession ?? costConv;
  let costEquivalentBadge: string | null = null;
  if (
    costEquivalent != null &&
    (costRealForBadge == null ||
      (costEquivalent > costRealForBadge &&
        Math.abs(costEquivalent - costRealForBadge) > 0.01))
  ) {
    const fmt =
      costEquivalent < 1 ? costEquivalent.toFixed(2) : costEquivalent.toFixed(1);
    costEquivalentBadge = `est $${fmt}`;
  }

  const costAriaLabel =
    costSession != null && costConv != null
      ? `Cost conversation ${costConv.toFixed(2)} session ${costSession.toFixed(2)}`
      : undefined;
  const ctxAriaLabel =
    ctxReal != null || ctxScaled != null
      ? `Context real ${ctxReal ?? "unknown"}% scaled ${ctxScaled ?? "unknown"}%`
      : undefined;

  return (
    <div className="flex flex-1 min-h-0 h-full">
      {/* Session sidebar */}
      <SessionSidebar
        panelVisible={panelVisible}
        activeSession={activeSession}
        openSessions={openSessions}
        onSelectSession={(name) => handleSelectSession(name, "sidebar")}
        onSessionCreated={handleSessionCreated}
        onSessionDeleted={handleSessionDeleted}
        onSessionRenamed={handleSessionRenamed}
      />

      {/* Terminal content area */}
      <div className="flex flex-col flex-1 min-w-0 h-full">
        {/* Toolbar — v2 uses mono uppercase chips with accent underline on active,
            v1 unchanged (caption-sized bordered buttons). */}
        {v2 ? (
          <div
            className="flex items-center justify-end gap-1.5 bg-pir-surface-0 border-b border-pir shrink-0"
            style={{ height: 30, padding: "0 12px" }}
          >
            <button
              type="button"
              onClick={toggleDiagnostics}
              className="pir-v2-tbbtn"
              data-active={diagnosticsInfo.active ? "true" : "false"}
              title={diagnosticsInfo.active ? "Stop terminal diagnostics" : "Start 30-minute terminal diagnostics"}
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 600,
                fontSize: 9,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                padding: "4px 7px",
                border: `1px solid ${diagnosticsInfo.active ? "hsl(var(--pir-accent))" : "var(--pir-border)"}`,
                borderRadius: "var(--radius-sm, 2px)",
                color: diagnosticsInfo.active ? "hsl(var(--pir-accent))" : "var(--pir-text-tertiary)",
                background: "transparent",
                display: "flex",
                alignItems: "center",
                gap: 4,
              }}
            >
              {diagnosticsInfo.active && (
                <span style={{ color: "hsl(var(--pir-accent))" }}>●</span>
              )}
              {diagnosticsInfo.active ? `Diag ${diagnosticsMinutesLeft}m` : "Diag 30m"}
            </button>
            <button
              type="button"
              onClick={markCurrentIssue}
              disabled={!diagnosticsInfo.active}
              className="pir-v2-tbbtn"
              title="Mark what you are seeing right now"
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 600,
                fontSize: 9,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                padding: "4px 7px",
                border: "1px solid var(--pir-border)",
                borderRadius: "var(--radius-sm, 2px)",
                color: "var(--pir-text-tertiary)",
                background: "transparent",
                opacity: diagnosticsInfo.active ? 1 : 0.4,
              }}
            >
              Mark
            </button>
            <button
              type="button"
              onClick={downloadTerminalDiagnostics}
              disabled={diagnosticsInfo.eventCount === 0}
              className="pir-v2-tbbtn"
              title="Export terminal diagnostics JSON"
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 600,
                fontSize: 9,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                padding: "4px 7px",
                border: "1px solid var(--pir-border)",
                borderRadius: "var(--radius-sm, 2px)",
                color: "var(--pir-text-tertiary)",
                background: "transparent",
                opacity: diagnosticsInfo.eventCount === 0 ? 0.4 : 1,
              }}
            >
              Export {diagnosticsInfo.eventCount > 0 ? `${diagnosticsInfo.eventCount} ↓` : ""}
            </button>
            <button
              type="button"
              onClick={() => setTerminalMetricsEnabled((value) => !value)}
              className="pir-v2-tbbtn"
              data-active={terminalMetricsEnabled ? "true" : "false"}
              title={terminalMetricsEnabled ? "Stop performance metrics polling" : "Start performance metrics polling"}
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 600,
                fontSize: 9,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                padding: "4px 7px",
                border: `1px solid ${terminalMetricsEnabled ? "hsl(var(--pir-accent))" : "var(--pir-border)"}`,
                borderRadius: "var(--radius-sm, 2px)",
                color: terminalMetricsEnabled ? "hsl(var(--pir-accent))" : "var(--pir-text-tertiary)",
                background: "transparent",
              }}
            >
              Perf
            </button>
            <span
              style={{
                color: "var(--pir-text-muted)",
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 500,
                fontSize: 9,
                lineHeight: 1,
              }}
              aria-hidden
            >
              ·
            </span>
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="pir-v2-tbbtn"
              title="Quick switch (Cmd+K)"
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 600,
                fontSize: 9,
                letterSpacing: "0.1em",
                textTransform: "uppercase",
                padding: "4px 7px",
                border: "1px solid var(--pir-border)",
                borderRadius: "var(--radius-sm, 2px)",
                color: "var(--pir-text-tertiary)",
                background: "transparent",
              }}
            >
              ⌘K
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-end gap-2 px-3 py-1 bg-pir-surface-0 border-b border-pir shrink-0">
            <button
              onClick={toggleDiagnostics}
              className={`px-2 py-0.5 text-caption border rounded font-mono ${diagnosticsInfo.active ? "text-amber-700 border-amber-400 bg-amber-50" : "text-pir-text-muted hover:text-pir-text-secondary border-pir"}`}
              title={diagnosticsInfo.active ? "Stop terminal diagnostics" : "Start 30-minute terminal diagnostics"}
            >
              {diagnosticsInfo.active ? `Diag ${diagnosticsMinutesLeft}m` : "Diag 30m"}
            </button>
            <button
              onClick={markCurrentIssue}
              disabled={!diagnosticsInfo.active}
              className="px-2 py-0.5 text-caption text-pir-text-muted hover:text-pir-text-secondary border border-pir rounded disabled:opacity-40"
              title="Mark what you are seeing right now"
            >
              Mark
            </button>
            <button
              onClick={downloadTerminalDiagnostics}
              disabled={diagnosticsInfo.eventCount === 0}
              className="px-2 py-0.5 text-caption text-pir-text-muted hover:text-pir-text-secondary border border-pir rounded disabled:opacity-40"
              title="Export terminal diagnostics JSON"
            >
              Export {diagnosticsInfo.eventCount > 0 ? diagnosticsInfo.eventCount : ""}
            </button>
            <button
              onClick={() => setTerminalMetricsEnabled((value) => !value)}
              className={`px-2 py-0.5 text-caption border rounded font-mono ${
                terminalMetricsEnabled
                  ? "text-pir-accent border-pir-accent bg-pir-accent/10"
                  : "text-pir-text-muted hover:text-pir-text-secondary border-pir"
              }`}
              title={terminalMetricsEnabled ? "Stop performance metrics polling" : "Start performance metrics polling"}
            >
              Perf
            </button>
            <button
              onClick={() => setPaletteOpen(true)}
              className="px-2 py-0.5 text-caption text-pir-text-muted hover:text-pir-text-secondary border border-pir rounded font-mono"
              title="Quick switch (Cmd+K)"
            >
              Cmd+K
            </button>
          </div>
        )}

        {terminalMetricsEnabled && (
          <div className="shrink-0 border-b border-pir bg-pir-surface-0 px-3 py-1.5 font-mono text-[10px] text-pir-text-tertiary">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="uppercase tracking-[0.14em] text-pir-text-muted">Perf</span>
              <span>rx:a <b className="text-pir-text-primary">{compactBytes(rxActive)}/s</b></span>
              <span>rx:h <b className="text-pir-text-primary">{compactBytes(rxHidden)}/s</b></span>
              <span>parse:p99 <b className="text-pir-text-primary">{compactMs(parseP99)}</b></span>
              <span>wheel <b className="text-pir-text-primary">{wheelText}</b>/s</span>
              <span>lat:p99 <b className="text-pir-text-primary">{latencyText}</b></span>
              <span>fanout:p99 <b className="text-pir-text-primary">{compactMs(activeServerMetrics?.fanout_duration_ms.p99)}</b></span>
              <span>api:p99 <b className="text-pir-text-primary">{compactMs(apiRttP99)}</b></span>
              <span>ws:rtt <b className="text-pir-text-primary">{compactMs(websocketRttP99)}</b></span>
              <span>net <b className="text-pir-text-primary">{compactMs(serverInternetP99)}</b></span>
              <span>loop <b className="text-pir-text-primary">{compactMs(eventLoopLagP99)}</b></span>
              <span>rss <b className="text-pir-text-primary">{processRssMb == null ? "—" : `${processRssMb.toFixed(0)}M`}</b></span>
              <span>fd <b className="text-pir-text-primary">{terminalMetrics?.process?.open_fd_count ?? "—"}</b></span>
              <span>ws <b className="text-pir-text-primary">{terminalMetrics?.live_websocket_count ?? "—"}</b></span>
              <span>pty <b className="text-pir-text-primary">{terminalMetrics?.live_pty_reader_count ?? "—"}</b></span>
              {latestNetworkStatus && (
                <span>probe <b className="text-pir-text-primary">{latestNetworkStatus}</b></span>
              )}
              {terminalMetricsError && (
                <span className="text-pir-error">metrics {terminalMetricsError}</span>
              )}
              {terminalNetworkError && (
                <span className="text-pir-error">network {terminalNetworkError}</span>
              )}
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-pir-text-muted">
              <span>hot: {openSessions.length}/{MAX_HOT_TERMINALS_PER_BROWSER}</span>
              <span>parked: {parkedSessionsCount}</span>
              <span>active scroll + typing</span>
              <span>gate: 20 listed · 2 hot · 1 fetch</span>
              <span>events {diagnosticsInfo.eventCount}{diagnosticsInfo.droppedEventCount ? ` · dropped ${diagnosticsInfo.droppedEventCount}` : ""}</span>
            </div>
          </div>
        )}

        {/* Terminal area */}
        <div className="flex-1 relative min-h-0">
          {openSessions.length === 0 || !activeSession ? (
            <div className="flex items-center justify-center h-full text-pir-text-muted px-4 text-center">
              <div>
                <p className="text-body mb-2">Select or create a session to start</p>
                <p className="text-caption text-pir-text-muted">
                  Press <kbd className="px-1 py-0.5 bg-pir-surface-1 rounded text-caption font-mono">Cmd+K</kbd> to quick switch
                </p>
              </div>
            </div>
          ) : (
            <>
              {openSessions.map((sessionName) => {
                const sessionMeta = sessionMetaByName.get(sessionName) ?? null;
                return (
                  <Terminal
                    key={sessionName}
                    sessionName={sessionName}
                    sessionProvider={sessionMeta?.provider}
                    isActive={sessionName === activeSession}
                    panelVisible={panelVisible}
                    onStatusChange={getStatusCallback(sessionName)}
                    onLifecycleEvent={getLifecycleCallback(sessionName)}
                    onPtyOutputParsed={getPtyOutputParsedCallback(sessionName)}
                    onAuthError={handleAuthError}
                    initialCommand={pendingCommands[sessionName]}
                    onInitialCommandSent={getCommandSentCallback(sessionName)}
                  />
                );
              })}
              {activeRestoreState ? (
                <div className="absolute left-3 right-3 top-3 z-20 max-w-3xl overflow-hidden">
                  <TerminalRestoreOverlay restore={activeRestoreState} />
                </div>
              ) : null}
            </>
          )}
        </div>

        {/* v2 footer strip — L5 loader + Model/Ctx/In/Out/$/Status/shells/clock.
            PR2 adds dual metrics + responsive truncation cascade:
              >1200px: full (1M badge + dual ctx + dual cost + labels)
              900-1200px: drop 1M badge + drop cost_session (conv only)
              600-900px: drop labels, keep values only
              <600px: drop scaled ctx + drop Out value
            Staleness fades the metrics group to 0.5 opacity when
            metrics_refreshed_at is older than 1 hour. */}
        {v2 && (
          <>
            <div
              className="shrink-0 flex items-center text-pir-text-tertiary bg-pir-surface-0 border-t border-pir"
              style={{
                height: 28,
                padding: "0 12px",
                gap: 10,
                fontFamily: "var(--pir-font-mono)",
                fontSize: 10,
                fontWeight: 500,
                lineHeight: 1,
              }}
            >
              <L5Loader size={18} />
              <span
                aria-hidden
                style={{ width: 1, height: 12, background: "var(--pir-border)" }}
              />
              <span
                className="flex items-center gap-1.5"
                style={{ opacity: metricsStale ? 0.5 : 1 }}
              >
                {showLabels && (
                  <span
                    className="text-pir-text-muted uppercase"
                    style={{ fontSize: 9, letterSpacing: "0.15em" }}
                  >
                    Model
                  </span>
                )}
                <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                  {modelName ? shortenModelForFooter(modelName) : "—"}
                </span>
                {showOneMBadge && modelName && hasOneMillionFooter(modelName) && (
                  <span style={{ color: "var(--pir-text-muted)" }}>·1M</span>
                )}
              </span>
              <span aria-hidden style={{ width: 1, height: 12, background: "var(--pir-border)" }} />
              <span
                className="flex items-center gap-1.5"
                style={{ opacity: metricsStale ? 0.5 : 1 }}
                aria-label={ctxAriaLabel}
              >
                {showLabels && (
                  <span
                    className="text-pir-text-muted uppercase"
                    style={{ fontSize: 9, letterSpacing: "0.15em" }}
                  >
                    Ctx
                  </span>
                )}
                <span className="pir-v2-ctx-meter">
                  <span style={{ width: `${Math.max(0, Math.min(100, ctxPct ?? 0))}%` }} />
                </span>
                <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                  {ctxText}
                </span>
              </span>
              <span aria-hidden style={{ width: 1, height: 12, background: "var(--pir-border)" }} />
              <span
                className="flex items-center gap-1.5"
                style={{ opacity: metricsStale ? 0.5 : 1 }}
              >
                {showLabels && (
                  <span
                    className="text-pir-text-muted uppercase"
                    style={{ fontSize: 9, letterSpacing: "0.15em" }}
                  >
                    In
                  </span>
                )}
                <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                  {fmtTokens(inTokens)}
                </span>
              </span>
              {showOutValue && (
                <span
                  className="flex items-center gap-1.5"
                  style={{ opacity: metricsStale ? 0.5 : 1 }}
                >
                  {showLabels && (
                    <span
                      className="text-pir-text-muted uppercase"
                      style={{ fontSize: 9, letterSpacing: "0.15em" }}
                    >
                      Out
                    </span>
                  )}
                  <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                    {fmtTokens(outTokens)}
                  </span>
                </span>
              )}
              <span
                className="flex items-center gap-1.5"
                style={{ opacity: metricsStale ? 0.5 : 1 }}
                aria-label={costAriaLabel}
              >
                {showLabels && (
                  <span
                    className="text-pir-text-muted uppercase"
                    style={{ fontSize: 9, letterSpacing: "0.15em" }}
                  >
                    $
                  </span>
                )}
                <span style={{ color: "hsl(var(--pir-accent))", fontWeight: 600 }}>
                  {costText}
                </span>
                {costEquivalentBadge && (
                  <span
                    className="text-pir-text-tertiary"
                    style={{
                      fontSize: 9,
                      fontWeight: 500,
                      letterSpacing: "0.02em",
                    }}
                    aria-label={`Equivalent API cost ${costEquivalentBadge}`}
                  >
                    ({costEquivalentBadge})
                  </span>
                )}
              </span>
              <div
                className="ml-auto flex items-center"
                style={{ gap: 10 }}
              >
                <span className="flex items-center gap-1.5">
                  <span
                    className="text-pir-text-muted uppercase"
                    style={{ fontSize: 9, letterSpacing: "0.15em" }}
                  >
                    Status
                  </span>
                  <span style={{ color: "hsl(var(--pir-success))", fontWeight: 600 }}>
                    {diagnosticsInfo.active ? "diagnostic" : "ready"}
                  </span>
                </span>
                <span>
                  <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                    {shellsCount}
                  </span>{" "}
                  hot · {parkedSessionsCount} parked
                </span>
                {clockNow && (
                  <span className="text-pir-text-primary" style={{ fontWeight: 600 }}>
                    {clockNow}
                  </span>
                )}
              </div>
            </div>
          </>
        )}
      </div>

      {/* Command Palette */}
      {paletteOpen && (
        <CommandPalette
          sessions={allSessions}
          onSelect={(name) => handleSelectSession(name, "palette")}
          onClose={() => setPaletteOpen(false)}
        />
      )}
    </div>
  );
}
