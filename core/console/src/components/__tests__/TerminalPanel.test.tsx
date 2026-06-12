import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/dynamic", () => ({
  default: () => (props: {
    sessionName: string;
    isActive: boolean;
    panelVisible?: boolean;
    onStatusChange?: (status: "connecting" | "connected" | "disconnected" | "error") => void;
    onLifecycleEvent?: (event: {
      phase:
        | "connect_started"
        | "direct_probe_completed"
        | "ticket_completed"
        | "preflight_completed"
        | "socket_created"
        | "socket_open"
        | "socket_error";
      attempt: number;
      durationMs?: number;
      elapsedMs?: number;
      openWaitMs?: number;
      transport?: "direct" | "tunnel";
      error?: string;
    }) => void;
    onPtyOutputParsed?: () => void;
  }) => (
    <div
      data-testid={`terminal-${props.sessionName}`}
      data-active={props.isActive ? "true" : "false"}
      data-visible={props.panelVisible ? "true" : "false"}
    >
      {props.sessionName}
      <button onClick={() => props.onStatusChange?.("connected")}>
        connect-{props.sessionName}
      </button>
      <button onClick={() => props.onStatusChange?.("disconnected")}>
        disconnect-{props.sessionName}
      </button>
      <button onClick={() => props.onPtyOutputParsed?.()}>
        parsed-{props.sessionName}
      </button>
      <button
        onClick={() =>
          props.onLifecycleEvent?.({
            phase: "ticket_completed",
            attempt: 0,
            durationMs: 42,
          })
        }
      >
        ticket-{props.sessionName}
      </button>
      <button
        onClick={() =>
          props.onLifecycleEvent?.({
            phase: "preflight_completed",
            attempt: 0,
            elapsedMs: 120,
            transport: "direct",
          })
        }
      >
        preflight-{props.sessionName}
      </button>
    </div>
  ),
}));

vi.mock("@/components/SessionSidebar", () => ({
  default: ({
    activeSession,
    onSelectSession,
    onSessionDeleted,
    onSessionRenamed,
  }: {
    activeSession: string | null;
    onSelectSession: (name: string) => void;
    onSessionDeleted?: (name: string) => void;
    onSessionRenamed?: (oldName: string, newName: string) => void;
  }) => (
    <div>
      <div data-testid="active-session">{activeSession ?? "none"}</div>
      <button onClick={() => onSelectSession("GetMarvisXBetter")}>switch-get</button>
      <button onClick={() => onSelectSession("HardWork")}>switch-hard</button>
      <button onClick={() => onSelectSession("ThirdSession")}>switch-third</button>
      <button onClick={() => onSessionDeleted?.("HardWork")}>delete-hard</button>
      <button onClick={() => onSessionRenamed?.("HardWork", "RenamedHardWork")}>rename-hard</button>
    </div>
  ),
}));

vi.mock("@/components/CommandPalette", () => ({
  default: () => null,
}));

vi.mock("@/lib/terminalDiagnostics", () => ({
  bootTerminalDiagnosticsFromLocation: vi.fn(),
  downloadTerminalDiagnostics: vi.fn(),
  getTerminalDiagnosticsChangeEventName: vi.fn(() => "pir-terminal-diagnostics-change"),
  getTerminalDiagnosticsInfo: vi.fn(() => ({
    active: false,
    startedAt: null,
    expiresAt: null,
    remainingMs: 0,
    eventCount: 0,
    droppedEventCount: 0,
    counters: [],
  })),
  getPendingTerminalDiagnosticsBatch: vi.fn(() => null),
  markTerminalDiagnostic: vi.fn(),
  markTerminalDiagnosticsBatchPosted: vi.fn(),
  recordCounterSample: vi.fn(),
  recordTerminalDiagnosticEvent: vi.fn(),
  registerTerminalDiagnosticsConsole: vi.fn(),
  startTerminalDiagnostics: vi.fn(),
  stopTerminalDiagnostics: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSessionByUUID: vi.fn(),
  getSessionMetrics: vi.fn(),
  getTerminalMetrics: vi.fn(),
  getTerminalNetworkProbe: vi.fn(),
  listSessions: vi.fn(),
  postTerminalMetricsBatch: vi.fn(),
}));

import {
  getSessionByUUID,
  getSessionMetrics,
  getTerminalMetrics,
  getTerminalNetworkProbe,
  listSessions,
} from "@/lib/api";
import {
  getTerminalDiagnosticsInfo,
  recordTerminalDiagnosticEvent,
} from "@/lib/terminalDiagnostics";
import type { Session } from "@/lib/types";
import TerminalPanel from "../TerminalPanel";

const baseSession: Session = {
  name: "GetMarvisXBetter",
  display_name: null,
  pinned: false,
  sort_order: 0,
  group_name: null,
  project_slug: null,
  session_uuid: "868ce2d4-2670-4a68-9132-294a67ba45f3",
  status: "idle",
  provider: "opencode",
  created_at: "2026-04-07T00:00:00Z",
  last_active: "2026-04-07T00:00:00Z",
  attached: false,
  hibernated: false,
  conversation_id: null,
  model: null,
  last_context_pct: null,
  last_cost_usd: null,
  last_message_count: null,
  auto_hibernate_minutes: 30,
  activity_state: "idle",
  cpu_pct: null,
  ram_mb: null,
  working_seconds: 0,
  created_epoch: 0,
  completed_at: null,
  agent_managed: false,
};

const hardWorkSession: Session = {
  ...baseSession,
  name: "HardWork",
  session_uuid: "11111111-2222-3333-4444-555555555555",
};

const thirdSession: Session = {
  ...baseSession,
  name: "ThirdSession",
  session_uuid: "22222222-3333-4444-5555-666666666666",
};

describe("TerminalPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    window.history.replaceState({}, "", "/terminal/868ce2d4-2670-4a68-9132-294a67ba45f3");
    vi.mocked(getSessionByUUID).mockResolvedValue(baseSession);
    vi.mocked(getSessionMetrics).mockResolvedValue({
      conversation_id: null,
      model: null,
      context_pct: 0,
      cost_usd: 0,
      message_count: 0,
      duration_minutes: 0,
      hibernated: false,
      auto_hibernate_minutes: 30,
    });
    vi.mocked(listSessions).mockResolvedValue([baseSession, hardWorkSession, thirdSession]);
    vi.mocked(getTerminalMetrics).mockResolvedValue({
      timestamp: 0,
      window_seconds: 60,
      live_websocket_count: 0,
      live_pty_reader_count: 0,
      sessions: {},
    });
    vi.mocked(getTerminalNetworkProbe).mockResolvedValue({
      client_host: "127.0.0.1",
      payload_bytes: 65_536,
      padding: "",
      server_internet_probe: {
        target: "cloudflare_trace",
        url: "https://www.cloudflare.com/cdn-cgi/trace",
        ok: true,
        status_code: 200,
        duration_ms: 100,
        bytes_received: 240,
        error: null,
      },
    });
  });

  it("keeps the previous terminal mounted when switching sessions", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toHaveAttribute("data-active", "true");
    });

    fireEvent.click(screen.getByText("switch-hard"));

    await waitFor(() => {
      expect(screen.getByTestId("terminal-HardWork")).toBeInTheDocument();
      expect(screen.getByTestId("terminal-HardWork")).toHaveAttribute("data-active", "true");
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toHaveAttribute("data-active", "false");
    });
  });

  it("refreshes the active footer metrics without waiting for a full sessions refetch", async () => {
    localStorage.setItem("marvisx:design-v2", "true");
    const codexSession: Session = {
      ...baseSession,
      provider: "codex",
      model: "gpt-5.5",
      last_context_pct: null,
      last_context_pct_real: null,
      last_input_tokens: null,
      last_output_tokens: null,
      last_reasoning_tokens: null,
      metrics_refreshed_at: null,
    };
    vi.mocked(getSessionByUUID).mockResolvedValue(codexSession);
    vi.mocked(listSessions).mockResolvedValue([codexSession]);
    vi.mocked(getSessionMetrics).mockResolvedValue({
      conversation_id: "019dd4c7-4573-7843-bdf7-2270a47ac3fc",
      model: "gpt-5.5",
      context_pct: 71,
      cost_usd: 0,
      message_count: 0,
      duration_minutes: 0,
      hibernated: false,
      auto_hibernate_minutes: 30,
      context_pct_real: 71,
      context_pct_scaled: null,
      cost_conversation_usd: 0,
      cost_session_usd: 0,
      input_tokens: 513_999_547,
      output_tokens: 1_374_234,
      reasoning_tokens: 483_897,
    });

    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(getSessionMetrics).toHaveBeenCalledWith("GetMarvisXBetter");
    });
    await waitFor(() => {
      expect(screen.getByText(/71%/)).toBeInTheDocument();
      expect(screen.getByText("514.0M")).toBeInTheDocument();
      expect(screen.getByText("1.4M")).toBeInTheDocument();
      expect(screen.getByText("$0.00")).toBeInTheDocument();
    });
    expect(listSessions).not.toHaveBeenCalled();
  });

  it("keeps active plus most recent HOT sessions with LRU promotion", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_fetch_succeeded",
        expect.objectContaining({ reason: "mount" }),
      );
    });

    fireEvent.click(screen.getByText("switch-hard"));
    await waitFor(() => {
      expect(screen.getByTestId("terminal-HardWork")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("switch-get"));
    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toHaveAttribute("data-active", "true");
    });

    fireEvent.click(screen.getByText("switch-third"));

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
      expect(screen.getByTestId("terminal-ThirdSession")).toBeInTheDocument();
      expect(screen.queryByTestId("terminal-HardWork")).not.toBeInTheDocument();
    });
  });

  it("renders a phase loader for a COLD session and never replays cached output", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("switch-hard"));

    await waitFor(() => {
      expect(screen.getByLabelText("Terminal restore status for HardWork")).toBeInTheDocument();
      expect(screen.getAllByText("opening session").length).toBeGreaterThan(0);
    });
    expect(screen.queryByText("cached output")).not.toBeInTheDocument();
    expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
      "terminal_cold_to_hot_started",
      expect.objectContaining({
        sessionName: "HardWork",
        restoreUi: "phase-loader",
      }),
    );

    fireEvent.click(screen.getByText("ticket-HardWork"));
    await waitFor(() => {
      expect(screen.getByText("requesting ticket and checking route")).toBeInTheDocument();
      expect(screen.getByText(/ticket 42ms/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("preflight-HardWork"));
    await waitFor(() => {
      expect(screen.getAllByText("connecting websocket").length).toBeGreaterThan(0);
      expect(screen.getByText(/preflight 120ms/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("connect-HardWork"));
    await waitFor(() => {
      expect(screen.getByText("attaching PTY and waiting for first output")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("parsed-HardWork"));
    await waitFor(() => {
      expect(screen.queryByLabelText("Terminal restore status for HardWork")).not.toBeInTheDocument();
    });
  });

  it("cancels pending cold activation when a session is demoted before parsed output", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_fetch_succeeded",
        expect.objectContaining({ reason: "mount" }),
      );
    });

    fireEvent.click(screen.getByText("switch-hard"));
    await waitFor(() => {
      expect(screen.getByTestId("terminal-HardWork")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("switch-third"));
    await waitFor(() => {
      expect(screen.getByTestId("terminal-ThirdSession")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("switch-get"));
    await waitFor(() => {
      expect(screen.queryByTestId("terminal-HardWork")).not.toBeInTheDocument();
    });
    expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
      "terminal_cold_to_hot_cancelled",
      expect.objectContaining({
        sessionName: "HardWork",
        reason: "demoted-before-pty-ready",
      }),
    );
  });

  it("clears the restore loader when the pending cold session is deleted", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_fetch_succeeded",
        expect.objectContaining({ reason: "mount" }),
      );
    });

    fireEvent.click(screen.getByText("switch-hard"));
    await waitFor(() => {
      expect(screen.getByLabelText("Terminal restore status for HardWork")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("delete-hard"));

    await waitFor(() => {
      expect(screen.queryByLabelText("Terminal restore status for HardWork")).not.toBeInTheDocument();
    });
  });

  it("renames a pending restore loader with the session", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_fetch_succeeded",
        expect.objectContaining({ reason: "mount" }),
      );
    });

    fireEvent.click(screen.getByText("switch-hard"));
    await waitFor(() => {
      expect(screen.getByLabelText("Terminal restore status for HardWork")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("rename-hard"));

    await waitFor(() => {
      expect(screen.getByLabelText("Terminal restore status for RenamedHardWork")).toBeInTheDocument();
    });
    expect(screen.queryByLabelText("Terminal restore status for HardWork")).not.toBeInTheDocument();
  });

  it("does not parse non-terminal routes as session names while hidden", async () => {
    window.history.replaceState({}, "", "/inbox/triage/files");

    render(<TerminalPanel panelVisible={false} />);

    await waitFor(() => {
      expect(listSessions).toHaveBeenCalled();
    });
    expect(screen.getByTestId("active-session")).toHaveTextContent("none");
    expect(screen.queryByTestId("terminal-files")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/inbox/triage/files");
  });

  it("applies known state-only session changes without a full sessions refresh", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(screen.getByTestId("terminal-GetMarvisXBetter")).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_fetch_succeeded",
        expect.objectContaining({ reason: "mount" }),
      );
    });

    vi.mocked(listSessions).mockClear();
    vi.mocked(recordTerminalDiagnosticEvent).mockClear();

    fireEvent(
      window,
      new CustomEvent("marvisx:sessions_changed", {
        detail: {
          type: "sessions_changed",
          event: "updated",
          session_name: "GetMarvisXBetter",
          state: "working",
        },
      }),
    );

    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_delta_applied",
        expect.objectContaining({
          sessionName: "GetMarvisXBetter",
          state: "working",
        }),
      );
    });
    expect(listSessions).not.toHaveBeenCalled();
  });

  it("falls back to a full sessions refresh when a state delta is missing locally", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_fetch_succeeded",
        expect.objectContaining({ reason: "mount" }),
      );
    });

    vi.mocked(listSessions).mockClear();
    vi.mocked(recordTerminalDiagnosticEvent).mockClear();

    fireEvent(
      window,
      new CustomEvent("marvisx:sessions_changed", {
        detail: {
          type: "sessions_changed",
          event: "updated",
          session_name: "UnknownSession",
          state: "working",
        },
      }),
    );

    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_delta_missing_session",
        expect.objectContaining({ sessionName: "UnknownSession" }),
      );
    });
    await waitFor(() => {
      expect(listSessions).toHaveBeenCalledTimes(1);
    });
  });

  it("still refreshes the full sessions list for structural session events", async () => {
    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
        "sessions_fetch_succeeded",
        expect.objectContaining({ reason: "mount" }),
      );
    });

    vi.mocked(listSessions).mockClear();

    fireEvent(
      window,
      new CustomEvent("marvisx:sessions_changed", {
        detail: {
          type: "sessions_changed",
          event: "created",
          session_name: "FreshSession",
        },
      }),
    );

    await waitFor(() => {
      expect(listSessions).toHaveBeenCalledTimes(1);
    });
  });

  it("clears stale path sessions that no longer exist", async () => {
    localStorage.setItem("marvis-last-terminal-session", JSON.stringify({ uuid: null, name: "files" }));
    window.history.replaceState({}, "", "/terminal/files");

    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(window.location.pathname).toBe("/terminal/");
    });
    expect(screen.getByTestId("active-session")).toHaveTextContent("none");
    expect(screen.queryByTestId("terminal-files")).not.toBeInTheDocument();
    expect(localStorage.getItem("marvis-last-terminal-session")).toBeNull();
  });

  it("keeps diagnostics metrics polling while the browser tab is hidden", async () => {
    vi.mocked(getTerminalDiagnosticsInfo).mockReturnValue({
      active: true,
      startedAt: Date.now(),
      expiresAt: Date.now() + 30_000,
      remainingMs: 30_000,
      eventCount: 1,
      droppedEventCount: 0,
      counters: [],
    });
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });

    render(<TerminalPanel panelVisible />);

    await waitFor(() => {
      expect(getTerminalMetrics).toHaveBeenCalled();
    });
    expect(recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
      "terminal_metrics_fetched",
      expect.objectContaining({
        hidden: true,
        pollSource: "diagnostics",
      }),
    );
  });
});
