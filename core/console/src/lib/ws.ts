import { WS_BASE_URL, DIRECT_WS_URL, DIRECT_WS_PROBE_URL } from "./config";
import { getTerminalTicket } from "./api";
import type { WSConnectionStatus } from "./types";

const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_DELAY_MS = 2000;
const MAX_DELAY_MS = 15000;

export interface TerminalWSCallbacks {
  onData: (data: Uint8Array) => void;
  onStatusChange: (status: WSConnectionStatus) => void;
  onAuthError: () => void;
  onPing?: (message: { id?: string; sent_at?: number }) => void;
  onLifecycleEvent?: (event: TerminalWSLifecycleEvent) => void;
  getTerminalSize?: () => { cols: number; rows: number };
}

export interface TerminalWSLifecycleEvent {
  phase:
    | "connect_started"
    | "direct_probe_completed"
    | "ticket_completed"
    | "preflight_completed"
    | "preflight_failed"
    | "socket_created"
    | "socket_open"
    | "socket_closed"
    | "socket_error"
    | "reconnect_scheduled";
  attempt: number;
  elapsedMs?: number;
  durationMs?: number;
  openWaitMs?: number;
  delayMs?: number;
  transport?: "direct" | "tunnel";
  cols?: number;
  rows?: number;
  code?: number;
  reason?: string;
  wasClean?: boolean;
  socketOpened?: boolean;
  error?: string;
}

export class ReconnectingTerminalWS {
  private ws: WebSocket | null = null;
  private sessionName: string;
  private callbacks: TerminalWSCallbacks;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private connecting = false;
  private pendingResize: { cols: number; rows: number } | null = null;
  private lastSentResize: { cols: number; rows: number } | null = null;
  isDirect = false;
  private forceTunnel = false;
  private probeController: AbortController | null = null;
  private onSessionsChanged: (() => void) | null = null;

  setSessionsChangedCallback(cb: (() => void) | null) {
    this.onSessionsChanged = cb;
  }

  constructor(sessionName: string, callbacks: TerminalWSCallbacks) {
    this.sessionName = sessionName;
    this.callbacks = callbacks;
  }

  async connect(): Promise<void> {
    if (this.closed || this.connecting) return;
    const connectStartedAt = performance.now();
    const attempt = this.reconnectAttempts;
    this.connecting = true;
    this.callbacks.onStatusChange("connecting");
    this.emitLifecycle({ phase: "connect_started", attempt });

    // Probe direct path + fetch ticket in parallel
    let wsBase: string;
    let ticket: string;
    try {
      const probeStartedAt = performance.now();
      const probePromise = this.probeDirectPath().then((result) => {
        this.emitLifecycle({
          phase: "direct_probe_completed",
          attempt,
          durationMs: performance.now() - probeStartedAt,
          transport: result === DIRECT_WS_URL ? "direct" : "tunnel",
        });
        return result;
      });
      const ticketStartedAt = performance.now();
      const ticketPromise = getTerminalTicket(this.sessionName).then((result) => {
        this.emitLifecycle({
          phase: "ticket_completed",
          attempt,
          durationMs: performance.now() - ticketStartedAt,
        });
        return result;
      });
      const [probeResult, ticketRes] = await Promise.all([
        probePromise,
        ticketPromise,
      ]);
      wsBase = probeResult;
      ticket = ticketRes.ticket;
      this.emitLifecycle({
        phase: "preflight_completed",
        attempt,
        elapsedMs: performance.now() - connectStartedAt,
        transport: wsBase === DIRECT_WS_URL ? "direct" : "tunnel",
      });
    } catch (err) {
      this.connecting = false;
      this.emitLifecycle({
        phase: "preflight_failed",
        attempt,
        elapsedMs: performance.now() - connectStartedAt,
        error: err instanceof Error ? err.message : String(err),
      });
      if (err instanceof Error && err.message === "Unauthorized") {
        this.callbacks.onAuthError();
        return;
      }
      this.scheduleReconnect();
      return;
    }

    // Guard: check closed after await (prevents phantom WS after unmount)
    if (this.closed) {
      this.connecting = false;
      return;
    }

    // Pass initial dimensions so the server can size the PTY before tmux attach
    const size = this.callbacks.getTerminalSize?.() ?? { cols: 80, rows: 24 };
    const url = `${wsBase}/terminal/ws?ticket=${encodeURIComponent(ticket)}&session=${encodeURIComponent(this.sessionName)}&cols=${size.cols}&rows=${size.rows}`;
    const socketCreatedAt = performance.now();
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    const transport = wsBase === DIRECT_WS_URL ? "direct" : "tunnel";
    let socketOpened = false;
    this.emitLifecycle({
      phase: "socket_created",
      attempt,
      elapsedMs: socketCreatedAt - connectStartedAt,
      transport,
      cols: size.cols,
      rows: size.rows,
    });

    ws.onopen = () => {
      socketOpened = true;
      this.isDirect = wsBase === DIRECT_WS_URL;
      this.forceTunnel = false;
      this.connecting = false;
      this.reconnectAttempts = 0;
      // Reset resize dedup on every fresh socket so the next resize actually
      // reaches tmux — the Terminal.tsx ws-connected handler is the single
      // source of truth for the post-reconnect SIGWINCH (Invariant I1: one
      // SIGWINCH per logical geometry change). Doing it here AND from the
      // ws-connected recovery produced two back-to-back redraws and fed the
      // scrollback-duplication bug.
      this.lastSentResize = null;
      this.callbacks.onStatusChange("connected");
      this.emitLifecycle({
        phase: "socket_open",
        attempt,
        elapsedMs: performance.now() - connectStartedAt,
        openWaitMs: performance.now() - socketCreatedAt,
        transport,
      });
      // Post-reconnect: dispatch hint so consumers can refetch full state and
      // recover any events missed while disconnected (e.g. session_renamed
      // broadcast that fired during the downtime). Plan 2026-05-21 AC6.
      if (attempt > 0) {
        window.dispatchEvent(new CustomEvent("marvisx:ws_reconnect", { detail: { attempt } }));
      }
    };

    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        // Binary: PTY output
        this.callbacks.onData(new Uint8Array(event.data));
      } else if (typeof event.data === "string") {
        // Text: control messages
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "ping") {
            this.callbacks.onPing?.({ id: msg.id, sent_at: msg.sent_at });
            ws.send(JSON.stringify({ type: "pong", id: msg.id, sent_at: msg.sent_at }));
          } else if (msg.type === "sessions_changed") {
            this.onSessionsChanged?.();
            window.dispatchEvent(new CustomEvent("marvisx:sessions_changed", { detail: msg }));
          } else if (msg.type === "ingest_changed") {
            window.dispatchEvent(new CustomEvent("marvisx:ingest_changed", { detail: msg }));
          }
        } catch {
          // Ignore malformed text
        }
      }
    };

    ws.onclose = (event) => {
      this.emitLifecycle({
        phase: "socket_closed",
        attempt,
        elapsedMs: performance.now() - connectStartedAt,
        openWaitMs: performance.now() - socketCreatedAt,
        transport,
        code: event.code,
        reason: event.reason,
        wasClean: event.wasClean,
        socketOpened,
      });
      // If direct WS failed before onopen, force tunnel on next retry
      if (wsBase === DIRECT_WS_URL && !this.isDirect) {
        this.forceTunnel = true;
      }
      this.ws = null;
      this.connecting = false;
      if (this.closed) return;

      this.callbacks.onStatusChange("disconnected");
      // 1008 = policy violation (bad auth), don't reconnect
      if (event.code === 1008) return;

      // 1012 = PTY terminated — reconnect immediately (fresh PTY will be created)
      if (event.code === 1012) {
        this.reconnectAttempts = 0;
        this.connect();
        return;
      }

      this.scheduleReconnect();
    };

    ws.onerror = () => {
      // Stop reconnecting if browser ran out of resources
      this.callbacks.onStatusChange("error");
      this.emitLifecycle({
        phase: "socket_error",
        attempt,
        elapsedMs: performance.now() - connectStartedAt,
        openWaitMs: performance.now() - socketCreatedAt,
        transport,
        socketOpened,
      });
    };

    this.ws = ws;
  }

  sendInput(data: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const encoder = new TextEncoder();
    const payload = encoder.encode(data);
    const frame = new Uint8Array(1 + payload.length);
    frame[0] = 0; // Input type
    frame.set(payload, 1);
    this.ws.send(frame);
  }

  sendResize(cols: number, rows: number, opts?: { force?: boolean }): void {
    // Guard against invalid dimensions (e.g., from hidden terminals with display:none)
    if (cols < 2 || rows < 2) return;
    const force = opts?.force ?? false;
    this.pendingResize = { cols, rows };
    // Skip if dimensions unchanged — prevents spurious SIGWINCH → tmux full redraw
    if (!force && this.lastSentResize &&
        this.lastSentResize.cols === cols &&
        this.lastSentResize.rows === rows) {
      return;
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.lastSentResize = { cols, rows };
    const encoder = new TextEncoder();
    const json = JSON.stringify({ cols, rows });
    const payload = encoder.encode(json);
    const frame = new Uint8Array(1 + payload.length);
    frame[0] = 1; // Resize type
    frame.set(payload, 1);
    this.ws.send(frame);
  }

  private scheduleReconnect(): void {
    if (this.closed) return;

    // Signal error state after several failures so the UI shows degraded indicator,
    // but keep retrying indefinitely — the connection may recover (CF Tunnel blip, etc.)
    if (this.reconnectAttempts === MAX_RECONNECT_ATTEMPTS) {
      this.callbacks.onStatusChange("error");
    }

    const delay = Math.min(
      BASE_DELAY_MS * Math.pow(2, Math.min(this.reconnectAttempts, 6)) +
        Math.random() * 1000,
      MAX_DELAY_MS
    );
    this.emitLifecycle({
      phase: "reconnect_scheduled",
      attempt: this.reconnectAttempts,
      delayMs: delay,
    });
    this.reconnectAttempts++;

    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, delay);
  }

  reconnectIfNeeded(): void {
    if (this.closed || this.connecting) return;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

    // Reset counter so we get fresh attempts after tab switch / visibility change
    this.reconnectAttempts = 0;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.connect();
  }

  forceReconnectForSnapshot(): void {
    if (this.closed || this.connecting) return;

    this.reconnectAttempts = 0;
    this.pendingResize = null;
    this.lastSentResize = null;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    const socket = this.ws;
    this.ws = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      try {
        socket.close();
      } catch {
        // Best-effort close; the fresh attach below is the recovery path.
      }
    }

    this.connect();
  }

  private async probeDirectPath(): Promise<string> {
    if (this.forceTunnel) {
      this.forceTunnel = false;
      return WS_BASE_URL;
    }

    this.probeController = new AbortController();
    const timeout = setTimeout(() => this.probeController?.abort(), 1500);
    try {
      const resp = await fetch(DIRECT_WS_PROBE_URL, {
        mode: "cors",
        cache: "no-store",
        signal: this.probeController.signal,
      });
      return resp.ok ? DIRECT_WS_URL : WS_BASE_URL;
    } catch {
      return WS_BASE_URL;
    } finally {
      clearTimeout(timeout);
      this.probeController = null;
    }
  }

  private emitLifecycle(event: TerminalWSLifecycleEvent): void {
    try {
      this.callbacks.onLifecycleEvent?.(event);
    } catch {
      // Diagnostics must never interfere with terminal connectivity.
    }
  }

  close(): void {
    this.closed = true;
    this.probeController?.abort();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
