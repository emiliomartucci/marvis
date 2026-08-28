"use client";

// v4.2.0 - 2026-04-01 - Zen Terminal: Reverted to Legacy Selection
//
// 1. Restored ShiftKey hack (allows native OS selection via click+drag)
// 2. Restored legacy selection buffer (500ms on mousedown, 100ms on mouseup)
// 3. Retained layout persistence (visibility:hidden) and scroll-desync fixes
// 4. Retained mouse tracking fallback for tmux reconnect

import React, { useCallback, useEffect, useMemo, useRef } from "react";
import { useTheme } from "next-themes";
import {
  Terminal as XTerm,
  type ILink,
  type ILinkHandler,
  type ILinkProvider,
} from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { ReconnectingTerminalWS, type TerminalWSLifecycleEvent } from "@/lib/ws";
import type { UploadResult } from "@/lib/api";
import type { SessionProvider, WSConnectionStatus } from "@/lib/types";
import { SgrMouseCoalescer, isSgrWheelInput } from "@/lib/sgrMouseCoalescer";
import {
  recordCounterSample,
  recordTerminalDiagnosticEvent,
  type TerminalInputKind,
} from "@/lib/terminalDiagnostics";
import { useDesignV2 } from "@/lib/useDesignV2";

const TERMINAL_SCROLLBACK_LINES = 10000;
const MARKDOWN_TERMINAL_LINK_PATTERN =
  /\[([^\]\r\n]{1,200})\]\(([^)\s]{1,2048})\)|"([^"\r\n]{1,200})"\[([^\]\s]{1,2048})\]/g;
const HTTP_LINK_SCHEME_PATTERN = /^https?:\/\//i;
const DOMAIN_LINK_HOST_PATTERN = /^(?:www\.|[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,})$/i;
const IPV4_LINK_HOST_PATTERN = /^(?:\d{1,3}\.){3}\d{1,3}$/;

type TerminalUploadResult = UploadResult & {
  project_relative_path?: string;
  ingest_path?: string;
};

function getSchemelessTerminalLinkDefaultScheme(target: string): "http" | "https" | null {
  try {
    const parsed = new URL(`https://${target}`);
    const hostname = parsed.hostname;
    if (hostname === "localhost" || IPV4_LINK_HOST_PATTERN.test(hostname)) return "http";
    if (DOMAIN_LINK_HOST_PATTERN.test(hostname)) return "https";
  } catch {
    return null;
  }
  return null;
}

function normalizeTerminalLinkTarget(target: string): string | null {
  const trimmed = target.trim();
  if (!trimmed) return null;

  const hasHttpScheme = HTTP_LINK_SCHEME_PATTERN.test(trimmed);
  const defaultScheme = hasHttpScheme ? null : getSchemelessTerminalLinkDefaultScheme(trimmed);
  if (!hasHttpScheme && !defaultScheme) return null;
  const candidate = hasHttpScheme ? trimmed : `${defaultScheme}://${trimmed}`;

  try {
    const url = new URL(candidate);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  } catch {
    return null;
  }

  if (hasHttpScheme) return trimmed;
  return candidate;
}

function openTerminalLink(uri: string) {
  window.open(uri, "_blank", "noopener,noreferrer");
}

const TERMINAL_LINK_HANDLER: ILinkHandler = {
  allowNonHttpProtocols: true,
  activate: (_event, uri) => {
    const normalizedUri = normalizeTerminalLinkTarget(uri);
    if (normalizedUri) openTerminalLink(normalizedUri);
  },
};

function createMarkdownTerminalLinkProvider(term: XTerm): ILinkProvider {
  return {
    provideLinks(bufferLineNumber, callback) {
      const bufferLine = term.buffer.active.getLine(bufferLineNumber - 1);
      const line = bufferLine?.translateToString(true);
      if (!line) {
        callback(undefined);
        return;
      }

      const links: ILink[] = [];
      for (const match of line.matchAll(MARKDOWN_TERMINAL_LINK_PATTERN)) {
        const label = match[1] ?? match[3];
        const target = match[2] ?? match[4];
        if (!label || !target || match.index === undefined) continue;

        const uri = normalizeTerminalLinkTarget(target);
        if (!uri) continue;

        const labelStartColumn = match.index + 2;
        const labelEndColumn = labelStartColumn + label.length - 1;
        links.push({
          range: {
            start: { x: labelStartColumn, y: bufferLineNumber },
            end: { x: labelEndColumn, y: bufferLineNumber },
          },
          text: label,
          decorations: {
            pointerCursor: true,
            underline: true,
          },
          activate: () => openTerminalLink(uri),
        });
      }

      callback(links.length ? links : undefined);
    },
  };
}

export function buildOpenCodeAutocompleteQuery(result: TerminalUploadResult): string | null {
  if (!result.project || !result.filename) return null;
  const relativePath = result.project_relative_path || `input/${result.filename}`;
  return `data_projects_link/${result.project}/${relativePath}`;
}

function normalizeUploadFilename(filename: string, fallbackBase: string): string {
  const trimmed = filename.trim();
  const candidate = trimmed || fallbackBase;
  const dotIndex = candidate.lastIndexOf(".");
  const stem = dotIndex > 0 ? candidate.slice(0, dotIndex) : candidate;
  const ext = dotIndex > 0 ? candidate.slice(dotIndex) : "";
  const normalizedStem = stem
    .replace(/\s+/g, "-")
    .replace(/[^A-Za-z0-9._-]/g, "_")
    .replace(/-+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "");
  return `${normalizedStem || fallbackBase}${ext}`;
}

async function injectOpenCodeUpload(ws: ReconnectingTerminalWS, result: TerminalUploadResult) {
  const query = buildOpenCodeAutocompleteQuery(result);
  if (!query) {
    ws.sendInput(result.path);
    return;
  }

  ws.sendInput(`@${query}`);
  await new Promise((resolve) => window.setTimeout(resolve, 250));
  ws.sendInput("\t");
}

const TERMINAL_DARK = {
  background: "#0b1520",
  foreground: "#e6edf3",
  cursor: "#3e7eff",
  selectionBackground: "#264f78",
  black: "#0b1520",
  red: "#f85149",
  green: "#3fb950",
  yellow: "#d29922",
  blue: "#58a6ff",
  magenta: "#bc8cff",
  cyan: "#39c5cf",
  white: "#e6edf3",
  brightBlack: "#484f58",
  brightRed: "#ffa198",
  brightGreen: "#56d364",
  brightYellow: "#e3b341",
  brightBlue: "#79c0ff",
  brightMagenta: "#d2a8ff",
  brightCyan: "#56d4dd",
  brightWhite: "#f0f6fc",
};

const TERMINAL_LIGHT = {
  background: "#F4F3EE",
  foreground: "#1a1628",
  cursor: "#C15F3C",
  selectionBackground: "#c5d2f6",
  black: "#2e2a3a",
  red: "#b91c1c",
  green: "#15803d",
  yellow: "#92400e",
  blue: "#1d4ed8",
  magenta: "#7e22ce",
  cyan: "#0e7490",
  white: "#44405a",
  brightBlack: "#6b6880",
  brightRed: "#dc2626",
  brightGreen: "#16a34a",
  brightYellow: "#b45309",
  brightBlue: "#2563eb",
  brightMagenta: "#9333ea",
  brightCyan: "#0891b2",
  brightWhite: "#1a1628",
};

const OPENCODE_TERMINAL_DARK = {
  ...TERMINAL_DARK,
  background: "#0f1b29",
  black: "#0f1b29",
};

const OPENCODE_TERMINAL_LIGHT = {
  ...TERMINAL_LIGHT,
  background: "#ffffff",
  black: "#ffffff",
};

// ─────────────────────────────────────────────────────────────────────────────
// Theme-v2 palettes — TE industrial (anthracite warm 30° + Riddim orange +
// forest green + bone text). Only applied when the `.theme-v2` class is live
// (tracked via useDesignV2()). OpenCode retains its v1 palette for now.
// ─────────────────────────────────────────────────────────────────────────────

const TERMINAL_DARK_V2 = {
  background: "#1C1917",       // anthracite hsl(30 5% 10%)
  foreground: "#F5F0E8",        // bone @ 98%
  cursor: "#F6581C",            // Riddim orange accent
  cursorAccent: "#1C1917",
  selectionBackground: "rgba(246, 88, 28, 0.28)",
  selectionForeground: "#F5F0E8",
  // ANSI 16 — warm-tinted, coherent with TE industrial v2 tokens.
  black: "#1C1917",
  red: "#E35848",               // vermillion (pir-error)
  green: "#4DAA85",             // forest bright (pir-secondary-bright)
  yellow: "#F3B13D",            // amber warm (pir-warning)
  blue: "#7FA8CC",              // muted blue (non elettrico)
  magenta: "#B48EAD",           // desaturated mauve
  cyan: "#6FA49A",              // teal muted
  white: "#D8CFC0",             // bone medium
  brightBlack: "#F6581C",       // Riddim orange — bold-black ANSI mapped to brand color so \x1b[1;30m stays readable on the anthracite background
  brightRed: "#FF6A55",
  brightGreen: "#6BC49D",
  brightYellow: "#FFC45C",
  brightBlue: "#9BC2E0",
  brightMagenta: "#C9A9BF",
  brightCyan: "#8AC0B5",
  brightWhite: "#F5F0E8",
};

const TERMINAL_LIGHT_V2 = {
  background: "#F0EAD9",        // paper hsl(36 22% 90%)
  foreground: "#1C1610",        // ink warm graphite @ 94%
  cursor: "#D44A10",            // Riddim orange darkened for paper
  cursorAccent: "#F0EAD9",
  selectionBackground: "rgba(212, 74, 16, 0.20)",
  selectionForeground: "#1C1610",
  black: "#1C1610",
  red: "#B13E30",
  green: "#2E7A5C",
  yellow: "#A87018",
  blue: "#4A7A99",
  magenta: "#8C6A80",
  cyan: "#457A70",
  white: "#4A3F30",
  brightBlack: "#D44A10",       // Riddim orange darkened — same brand mapping for bold-black on paper
  brightRed: "#D44A3A",
  brightGreen: "#4DAA85",
  brightYellow: "#C88A28",
  brightBlue: "#6F9AB8",
  brightMagenta: "#A88A9A",
  brightCyan: "#6A9A90",
  brightWhite: "#1C1610",
};

function getTerminalTheme(
  sessionProvider: SessionProvider,
  resolvedTheme: string | undefined,
  v2: boolean,
) {
  // OpenCode keeps its v1 palette — a dedicated v2 variant can land in a
  // follow-up PR once the core design has stabilised.
  if (sessionProvider === "opencode") {
    return resolvedTheme === "light" ? OPENCODE_TERMINAL_LIGHT : OPENCODE_TERMINAL_DARK;
  }
  if (v2) {
    return resolvedTheme === "light" ? TERMINAL_LIGHT_V2 : TERMINAL_DARK_V2;
  }
  return resolvedTheme === "light" ? TERMINAL_LIGHT : TERMINAL_DARK;
}

function getTerminalBackground(
  sessionProvider: SessionProvider,
  resolvedTheme: string | undefined,
  v2: boolean,
) {
  return getTerminalTheme(sessionProvider, resolvedTheme, v2).background;
}

const GEOMETRY_SYNC_SETTLE_MS = 75;
// Slow-settling layouts (split-view macOS, tab return, theme switch async font load)
// can leave geometry not yet stable after the 75ms settle pass. For force=true
// passes (mount + explicit recovery) we keep retrying out to ~600ms so the final
// fit captures the real container size — restoring the multi-delay behaviour from
// before commit 611e6c1 without the cost of always running 5 passes.
const GEOMETRY_SYNC_RECOVERY_MS = [300, 600] as const;
const SNAPSHOT_RECONNECT_SETTLE_MS = 250;
const RESIZE_SEND_SETTLE_MS = 400;
// Max PTY bytes buffered while a pane is not active/visible. Below this, on
// return we flush the ring locally (no WS teardown). Above it, we discard the
// ring and fall back to the forced snapshot reconnect. Kept conservative (64KB,
// not the 256KB of the capacity-leveling plan) so a single drain xterm.write
// stays a few ms on the DOM renderer rather than blocking the main thread.
const HIDDEN_RING_CAP_BYTES = 64 * 1024;

function buildTerminalGeometryFingerprint(container: HTMLDivElement) {
  const rect = container.getBoundingClientRect();
  const width = Math.round(rect.width);
  const height = Math.round(rect.height);
  const devicePixelRatio = Math.round((window.devicePixelRatio || 1) * 100) / 100;
  const viewportScale = Math.round((window.visualViewport?.scale || 1) * 100) / 100;
  return {
    width,
    height,
    devicePixelRatio,
    viewportScale,
    fingerprint: `${width}x${height}@${devicePixelRatio}/${viewportScale}`,
  };
}

function isMacPlatform() {
  return /Mac|iPhone|iPad|iPod/i.test(navigator.platform);
}

function isPrintableOptionInput(event: KeyboardEvent) {
  if (event.type !== "keydown") return false;
  if (!isMacPlatform()) return false;
  if (!event.altKey || event.ctrlKey || event.metaKey) return false;
  if (event.isComposing || event.key === "Dead") return false;
  if (event.key.length !== 1) return false;
  return !/[\u0000-\u001f\u007f]/.test(event.key);
}

const SGR_MOUSE_RE = /^\x1b\[<(\d+);\d+;\d+[Mm]$/;

function detectTerminalInputKind(data: string): TerminalInputKind {
  if (data === "\r") return "enter";
  if (SGR_MOUSE_RE.test(data)) return "sgr";
  if (data.length > 10 && !/^[\u0000-\u001f\u007f]+$/.test(data)) return "paste";
  if (data.length === 1 && /^[\x20-\x7e]$/.test(data)) return "text";
  return "control";
}

function buildTerminalDiagnosticSnapshot({
  sessionName,
  sessionProvider,
  resolvedTheme,
  isActive,
  panelVisible,
  term,
  container,
}: {
  sessionName: string;
  sessionProvider: SessionProvider;
  resolvedTheme: string | undefined;
  isActive: boolean;
  panelVisible: boolean;
  term: XTerm | null;
  container: HTMLDivElement | null;
}) {
  const xtermEl = term?.element as HTMLElement | null;
  const screenEl = xtermEl?.querySelector(".xterm-screen") as HTMLElement | null;
  const viewportEl = xtermEl?.querySelector(".xterm-viewport") as HTMLElement | null;
  const rowsEl = xtermEl?.querySelector(".xterm-rows") as HTMLElement | null;
  const rect = (element: HTMLElement | null) => {
    if (!element) return null;
    const box = element.getBoundingClientRect();
    return {
      width: Math.round(box.width),
      height: Math.round(box.height),
      top: Math.round(box.top),
      left: Math.round(box.left),
    };
  };
  const background = (element: HTMLElement | null) => {
    if (!element) return null;
    return window.getComputedStyle(element).backgroundColor;
  };

  return {
    sessionName,
    sessionProvider,
    resolvedTheme: resolvedTheme ?? null,
    isActive,
    panelVisible,
    hidden: document.hidden,
    devicePixelRatio: window.devicePixelRatio ?? null,
    viewportScale: window.visualViewport?.scale ?? null,
    cols: term?.cols ?? null,
    rows: term?.rows ?? null,
    bufferLength: term?.buffer.active.length ?? null,
    baseY: term?.buffer.active.baseY ?? null,
    cursorY: term?.buffer.active.cursorY ?? null,
    canvasCount: xtermEl?.querySelectorAll("canvas").length ?? 0,
    viewportScrollTop: viewportEl?.scrollTop ?? null,
    containerRect: rect(container),
    xtermRect: rect(xtermEl),
    screenRect: rect(screenEl),
    rowsRect: rect(rowsEl),
    containerBackground: background(container),
    xtermBackground: background(xtermEl),
    screenBackground: background(screenEl),
    viewportBackground: background(viewportEl),
  };
}

interface TerminalProps {
  sessionName: string;
  sessionProvider?: SessionProvider;
  isActive: boolean;
  panelVisible?: boolean;
  onStatusChange?: (status: WSConnectionStatus) => void;
  onLifecycleEvent?: (event: TerminalWSLifecycleEvent) => void;
  onPtyOutputParsed?: () => void;
  onAuthError?: () => void;
  initialCommand?: string;
  onInitialCommandSent?: () => void;
}

function TerminalInner({
  sessionName,
  sessionProvider = "claude",
  isActive,
  panelVisible = true,
  onStatusChange,
  onLifecycleEvent,
  onPtyOutputParsed,
  onAuthError,
  initialCommand,
  onInitialCommandSent,
}: TerminalProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XTerm | null>(null);
  const wsRef = useRef<ReconnectingTerminalWS | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);

  const onStatusChangeRef = useRef(onStatusChange);
  onStatusChangeRef.current = onStatusChange;
  const onLifecycleEventRef = useRef(onLifecycleEvent);
  onLifecycleEventRef.current = onLifecycleEvent;
  const onPtyOutputParsedRef = useRef(onPtyOutputParsed);
  onPtyOutputParsedRef.current = onPtyOutputParsed;
  const onAuthErrorRef = useRef(onAuthError);
  onAuthErrorRef.current = onAuthError;

  const { resolvedTheme } = useTheme();
  const v2 = useDesignV2();
  const sessionNameRef = useRef(sessionName);
  sessionNameRef.current = sessionName;
  const resolvedThemeRef = useRef(resolvedTheme);
  resolvedThemeRef.current = resolvedTheme;

  const isActiveRef = useRef(isActive);
  isActiveRef.current = isActive;
  const panelVisibleRef = useRef(panelVisible);
  panelVisibleRef.current = panelVisible;
  const sessionProviderRef = useRef(sessionProvider);
  sessionProviderRef.current = sessionProvider;
  // Memoized so the theme-swap useEffect has a stable identity across renders
  // that don't actually flip light/dark/v2 — avoids spurious re-runs.
  const terminalTheme = useMemo(
    () => getTerminalTheme(sessionProvider, resolvedTheme, v2),
    [sessionProvider, resolvedTheme, v2],
  );
  const terminalBackground = useMemo(
    () => getTerminalBackground(sessionProvider, resolvedTheme, v2),
    [sessionProvider, resolvedTheme, v2],
  );

  const initialCommandRef = useRef(initialCommand);
  const onInitialCommandSentRef = useRef(onInitialCommandSent);
  onInitialCommandSentRef.current = onInitialCommandSent;
  const initialCommandSentRef = useRef(false);
  const connectedAtRef = useRef<number>(0);
  const watchingForPromptRef = useRef(false);
  const syncTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const syncRecoveryTimeoutsRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  const syncRafRef = useRef<number | null>(null);
  const syncRunIdRef = useRef(0);
  const lastResizeLogAtRef = useRef(0);
  const lastAppliedGeometryRef = useRef<string | null>(null);
  const lastObservedGeometryRef = useRef<string | null>(null);
  // Hidden-pane FIFO ring. While a pane is not active/visible we buffer the
  // already-remapped output strings (NOT raw bytes — the live path runs them
  // through remapClaudePalette's stateful streaming TextDecoder + 256-color
  // remap; buffering raw would corrupt colors and split multibyte UTF-8). On
  // return we flush locally (no WS teardown, no capture-pane). Only on overflow
  // do we fall back to the forced snapshot reconnect. Replaces the old
  // droppedWhileHiddenRef drop-frames shortcut.
  const hiddenRingChunksRef = useRef<string[]>([]);
  const hiddenRingBytesRef = useRef(0);
  const ringOverflowedRef = useRef(false);
  const drainingRef = useRef(false);
  const drainHiddenRingRef = useRef<(() => void) | null>(null);
  const snapshotReconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stableResizeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingStableResizeRef = useRef<{
    cols: number;
    rows: number;
    force: boolean;
    fingerprint: string | null;
  } | null>(null);
  const pendingInputByKindRef = useRef<Record<TerminalInputKind, number[]>>({
    text: [],
    enter: [],
    sgr: [],
    paste: [],
    control: [],
  });

  const isActiveForMetrics = useCallback(() => {
    return isActiveRef.current && panelVisibleRef.current && !document.hidden;
  }, []);

  const recordPendingInputLatency = useCallback(() => {
    const now = performance.now();
    const pending = pendingInputByKindRef.current;
    let oldestKind: TerminalInputKind | null = null;
    let oldestTs = Number.POSITIVE_INFINITY;
    for (const kind of Object.keys(pending) as TerminalInputKind[]) {
      pending[kind] = pending[kind].filter((ts) => now - ts <= 5000);
      const first = pending[kind][0];
      if (first !== undefined && first < oldestTs) {
        oldestTs = first;
        oldestKind = kind;
      }
    }
    if (!oldestKind) return;
    pending[oldestKind].shift();
    recordCounterSample(
      "send_recv_latency_ms",
      now - oldestTs,
      isActiveForMetrics(),
      { sessionName: sessionNameRef.current, kind: oldestKind },
    );
  }, [isActiveForMetrics]);

  const recordWheelInputQueued = useCallback((data: string) => {
    if (!isSgrWheelInput(data)) return;
    recordCounterSample("wheel_events_pre_coalesce", 1, isActiveForMetrics(), {
      sessionName: sessionNameRef.current,
      kind: "sgr",
    });
  }, [isActiveForMetrics]);

  const trackTerminalInput = useCallback((data: string) => {
    const kind = detectTerminalInputKind(data);
    const active = isActiveForMetrics();
    pendingInputByKindRef.current[kind].push(performance.now());
    pendingInputByKindRef.current[kind] = pendingInputByKindRef.current[kind].slice(-200);
    recordCounterSample(
      "input_bytes_sent",
      new TextEncoder().encode(data).byteLength,
      active,
      { sessionName: sessionNameRef.current, kind },
    );
    if (isSgrWheelInput(data)) {
      recordCounterSample("wheel_events_per_sec", 1, active, {
        sessionName: sessionNameRef.current,
        kind,
      });
      recordCounterSample("wheel_events_post_coalesce", 1, active, {
        sessionName: sessionNameRef.current,
        kind,
      });
    }
  }, [isActiveForMetrics]);

  const cancelScheduledTerminalSync = useCallback(() => {
    if (syncTimeoutRef.current) clearTimeout(syncTimeoutRef.current);
    for (const t of syncRecoveryTimeoutsRef.current) clearTimeout(t);
    syncRecoveryTimeoutsRef.current = [];
    if (syncRafRef.current) cancelAnimationFrame(syncRafRef.current);
    syncTimeoutRef.current = null;
    syncRafRef.current = null;
  }, []);

  const cancelForcedSnapshotReconnect = useCallback(() => {
    if (snapshotReconnectTimeoutRef.current) {
      clearTimeout(snapshotReconnectTimeoutRef.current);
      snapshotReconnectTimeoutRef.current = null;
    }
  }, []);

  const scheduleForcedSnapshotReconnect = useCallback((reason: string) => {
    if (!ringOverflowedRef.current) return false;

    ringOverflowedRef.current = false;
    cancelForcedSnapshotReconnect();
    recordTerminalDiagnosticEvent("terminal_snapshot_reconnect_scheduled", {
      sessionName: sessionNameRef.current,
      sessionProvider: sessionProviderRef.current,
      reason,
      delayMs: SNAPSHOT_RECONNECT_SETTLE_MS,
    });

    snapshotReconnectTimeoutRef.current = setTimeout(() => {
      snapshotReconnectTimeoutRef.current = null;
      if (!termRef.current || document.hidden || !panelVisibleRef.current || !isActiveRef.current) {
        ringOverflowedRef.current = true;
        return;
      }

      recordTerminalDiagnosticEvent("terminal_snapshot_reconnect_forced", {
        sessionName: sessionNameRef.current,
        sessionProvider: sessionProviderRef.current,
        reason,
      });
      wsRef.current?.forceReconnectForSnapshot();
    }, SNAPSHOT_RECONNECT_SETTLE_MS);

    return true;
  }, [cancelForcedSnapshotReconnect]);

  const cancelStableResizeSend = useCallback(() => {
    if (stableResizeTimeoutRef.current) {
      clearTimeout(stableResizeTimeoutRef.current);
      stableResizeTimeoutRef.current = null;
    }
    pendingStableResizeRef.current = null;
  }, []);

  const scheduleStableResizeSend = useCallback((cols: number, rows: number, opts?: { force?: boolean }) => {
    if (document.hidden || !panelVisibleRef.current || !isActiveRef.current) return;
    if (cols < 2 || rows < 2) return;

    const container = containerRef.current;
    let fingerprint: string | null = null;
    if (container) {
      const geometry = buildTerminalGeometryFingerprint(container);
      if (geometry.width < 2 || geometry.height < 2) return;
      fingerprint = geometry.fingerprint;
    }

    if (stableResizeTimeoutRef.current) {
      clearTimeout(stableResizeTimeoutRef.current);
    }
    pendingStableResizeRef.current = {
      cols,
      rows,
      force: opts?.force ?? false,
      fingerprint,
    };

    stableResizeTimeoutRef.current = setTimeout(() => {
      stableResizeTimeoutRef.current = null;
      const pending = pendingStableResizeRef.current;
      pendingStableResizeRef.current = null;
      if (!pending || document.hidden || !panelVisibleRef.current || !isActiveRef.current) return;

      const currentContainer = containerRef.current;
      if (pending.fingerprint && currentContainer) {
        const currentGeometry = buildTerminalGeometryFingerprint(currentContainer);
        if (currentGeometry.fingerprint !== pending.fingerprint) {
          recordTerminalDiagnosticEvent("terminal_resize_deferred_unstable", {
            sessionName: sessionNameRef.current,
            provider: sessionProviderRef.current,
            cols: pending.cols,
            rows: pending.rows,
            expectedFingerprint: pending.fingerprint,
            actualFingerprint: currentGeometry.fingerprint,
          });
          return;
        }
      }

      recordTerminalDiagnosticEvent("terminal_resize_sent", {
        sessionName: sessionNameRef.current,
        cols: pending.cols,
        rows: pending.rows,
        provider: sessionProviderRef.current,
      });
      if (pending.force) {
        wsRef.current?.sendResize(pending.cols, pending.rows, { force: true });
      } else {
        wsRef.current?.sendResize(pending.cols, pending.rows);
      }
    }, RESIZE_SEND_SETTLE_MS);
  }, []);

  const runTerminalSync = useCallback(({
    scroll = false,
    focus = false,
    reason = "unspecified",
    force = false,
    reconnect = false,
    phase = "immediate",
  }: {
    scroll?: boolean;
    focus?: boolean;
    reason?: string;
    force?: boolean;
    reconnect?: boolean;
    phase?: "immediate" | "settle";
  } = {}) => {
    if (document.hidden || !panelVisibleRef.current || !isActiveRef.current) return;

    const container = containerRef.current;
    const term = termRef.current;
    const fitAddon = fitAddonRef.current;
    if (!container || !term || !fitAddon) return;

    const geometry = buildTerminalGeometryFingerprint(container);
    if (geometry.width < 2 || geometry.height < 2) return;

    const geometryChanged = geometry.fingerprint !== lastAppliedGeometryRef.current;
    if (!geometryChanged && !force) return;

    if (reconnect && phase === "immediate") wsRef.current?.reconnectIfNeeded();

    const prevCols = term.cols;
    const prevRows = term.rows;
    fitAddon.fit();
    const sizeChanged = term.cols !== prevCols || term.rows !== prevRows;

    // Send resize ONLY when geometry actually changed. A force-sync
    // pass (theme update, mount, active-visible recovery) must apply
    // fit()+refresh+scroll/focus locally, but it must NOT push a
    // redundant resize to tmux — the server re-emits a full redraw
    // on every resize, which races the just-rendered capture-pane
    // snapshot and garbles the pane ~500ms after a HOT switch.
    if (!sizeChanged && geometryChanged) {
      scheduleStableResizeSend(term.cols, term.rows, { force: true });
    }

    term.refresh(0, Math.max(term.rows - 1, 0));
    if (scroll) term.scrollToBottom();
    if (focus) term.focus();

    lastAppliedGeometryRef.current = geometry.fingerprint;
    lastObservedGeometryRef.current = geometry.fingerprint;

    recordTerminalDiagnosticEvent("terminal_sync_applied", {
      reason,
      phase,
      force,
      reconnect,
      geometryChanged,
      geometryFingerprint: geometry.fingerprint,
      ...buildTerminalDiagnosticSnapshot({
        sessionName: sessionNameRef.current,
        sessionProvider: sessionProviderRef.current,
        resolvedTheme: resolvedThemeRef.current,
        isActive: isActiveRef.current,
        panelVisible: panelVisibleRef.current,
        term,
        container,
      }),
    });
  }, [scheduleStableResizeSend]);

  const scheduleTerminalSync = useCallback(({
    scroll = false,
    focus = false,
    reason = "unspecified",
    force = false,
    reconnect = false,
  }: {
    scroll?: boolean;
    focus?: boolean;
    reason?: string;
    force?: boolean;
    reconnect?: boolean;
  } = {}) => {
    recordTerminalDiagnosticEvent("terminal_sync_scheduled", {
      sessionName: sessionNameRef.current,
      sessionProvider: sessionProviderRef.current,
      resolvedTheme: resolvedThemeRef.current ?? null,
      delays: force
        ? [0, GEOMETRY_SYNC_SETTLE_MS, ...GEOMETRY_SYNC_RECOVERY_MS]
        : [0, GEOMETRY_SYNC_SETTLE_MS],
      scroll,
      focus,
      reason,
      force,
      reconnect,
    });
    const runId = ++syncRunIdRef.current;
    cancelScheduledTerminalSync();

    const schedulePass = (phase: "immediate" | "settle") => {
      const rafId = requestAnimationFrame(() => {
        if (syncRafRef.current === rafId) {
          syncRafRef.current = null;
        }
        if (runId !== syncRunIdRef.current) return;
        if (phase === "settle") {
          syncTimeoutRef.current = null;
        }
        runTerminalSync({
          scroll,
          focus,
          reason,
          force,
          reconnect,
          phase,
        });
      });
      syncRafRef.current = rafId;
    };

    schedulePass("immediate");
    syncTimeoutRef.current = setTimeout(() => {
      schedulePass("settle");
    }, GEOMETRY_SYNC_SETTLE_MS);
    // Recovery passes only for force=true (mount + explicit recovery requests).
    // Catches slow-settling layouts where the 75ms settle pass fires before the
    // container reaches its final size — split-view, tab return, async font load.
    if (force) {
      for (const delay of GEOMETRY_SYNC_RECOVERY_MS) {
        syncRecoveryTimeoutsRef.current.push(
          setTimeout(() => schedulePass("settle"), delay),
        );
      }
    }
  }, [cancelScheduledTerminalSync, runTerminalSync]);

  const requestTerminalRecovery = useCallback(({
    reason,
    scroll = true,
    focus = true,
    reconnect = false,
  }: {
    reason: string;
    scroll?: boolean;
    focus?: boolean;
    reconnect?: boolean;
  }) => {
    if (!termRef.current || document.hidden || !panelVisibleRef.current || !isActiveRef.current) return;
    // Fast path: flush the hidden ring locally (no WS teardown). On overflow the
    // ring stays empty + ringOverflowedRef set, so scheduleForcedSnapshotReconnect
    // below fires the (now rare) capture-pane reconnect.
    if (reconnect) drainHiddenRingRef.current?.();
    const snapshotReconnectScheduled = reconnect && (
      scheduleForcedSnapshotReconnect(reason) ||
      snapshotReconnectTimeoutRef.current !== null
    );
    scheduleTerminalSync({
      reason,
      scroll,
      focus,
      force: true,
      reconnect: reconnect && !snapshotReconnectScheduled,
    });
    // xterm 5.x can leave the pane visually corrupted after a big buffer write
    // (capture-pane snapshot replay, tab return, window focus): the buffer
    // holds the right content but the rendered DOM keeps stale cells. The
    // synchronous refresh()+scrollToBottom() inside runTerminalSync sometimes
    // fires before the parser has finished applying the write, so the repaint
    // sees the old buffer. Force a full repaint AND a viewport re-attach on
    // the next animation frame, after DOM flush. Pure client-side, no WS
    // message → no race with the PTY reader.
    requestAnimationFrame(() => {
      const term = termRef.current;
      if (!term) return;
      try {
        term.refresh(0, Math.max(term.rows - 1, 0));
      } catch {
        // refresh throws if the renderer was disposed mid-frame; harmless.
      }
      if (scroll) term.scrollToBottom();
    });
  }, [scheduleForcedSnapshotReconnect, scheduleTerminalSync]);

  const scheduleGeometryChangeSync = useCallback((source: string) => {
    if (document.hidden || !panelVisibleRef.current || !isActiveRef.current) return;

    const container = containerRef.current;
    if (!container) return;

    const geometry = buildTerminalGeometryFingerprint(container);
    if (geometry.width < 2 || geometry.height < 2) return;
    if (geometry.fingerprint === lastObservedGeometryRef.current) return;

    lastObservedGeometryRef.current = geometry.fingerprint;
    if (Date.now() - lastResizeLogAtRef.current > 500) {
      lastResizeLogAtRef.current = Date.now();
      recordTerminalDiagnosticEvent("terminal_resize_observed", {
        sessionName: sessionNameRef.current,
        width: geometry.width,
        height: geometry.height,
        provider: sessionProviderRef.current,
        source,
        devicePixelRatio: geometry.devicePixelRatio,
        viewportScale: geometry.viewportScale,
      });
    }

    scheduleTerminalSync({ reason: "geometry-changed" });
  }, [scheduleTerminalSync]);

  // === MAIN SETUP EFFECT ===
  useEffect(() => {
    if (!containerRef.current) return;

    const term = new XTerm({
      cursorBlink: true,
      fontFamily: "JetBrains Mono, monospace",
      fontSize: 14,
      lineHeight: 1.2,
      scrollback: TERMINAL_SCROLLBACK_LINES,
      cols: 80,
      rows: 24,
      allowProposedApi: true,
      macOptionIsMeta: false,
      linkHandler: TERMINAL_LINK_HANDLER,
      theme: terminalTheme,
    });

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon((_event, uri) => openTerminalLink(uri));

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);
    term.open(containerRef.current);
    const markdownLinkProviderDisposable = term.registerLinkProvider(
      createMarkdownTerminalLinkProvider(term),
    );

    // Initial fit: only run when the container has real dimensions, otherwise
    // xterm.js stays at the safe 80×24 default. Restored from commit b0aeb97
    // (lost in 4aea24f). Without this guard, fit() on a not-yet-laid-out
    // container can collapse cols below 80 — the WS query string then sends
    // those bad cols to the server, tmux creates the pane narrow, and the
    // first banner the TUI prints (e.g. Claude Code) is wrapped permanently
    // in tmux scrollback at the wrong width.
    const initialRect = containerRef.current.getBoundingClientRect();
    const initialContainerReady = initialRect.width > 10 && initialRect.height > 10;
    if (initialContainerReady) {
      fitAddon.fit();
    }

    termRef.current = term;
    fitAddonRef.current = fitAddon;
    lastAppliedGeometryRef.current = null;
    lastObservedGeometryRef.current = null;
    recordTerminalDiagnosticEvent("terminal_mount", {
      ...buildTerminalDiagnosticSnapshot({
        sessionName,
        sessionProvider,
        resolvedTheme,
        isActive,
        panelVisible,
        term,
        container: containerRef.current,
      }),
    });
    scheduleTerminalSync({ reason: "mount", force: true });

    // --- OSC 52 clipboard support ---
    term.parser.registerOscHandler(52, (data: string) => {
      const idx = data.indexOf(";");
      if (idx === -1) return true;
      try {
        navigator.clipboard.writeText(atob(data.slice(idx + 1))).catch(() => {});
      } catch { /* ignore decode errors */ }
      return true;
    });

    // --- RESTORED: Force shiftKey on desktop for tmux mouse mode ---
    const isTouchDevice = "ontouchstart" in window || navigator.maxTouchPoints > 0;
    const xtermEl = term.element;
    if (xtermEl && !isTouchDevice) {
      for (const evtType of ["mousedown", "mousemove", "mouseup"]) {
        xtermEl.addEventListener(evtType, ((e: MouseEvent) => {
          if (e.button === 0 && !e.shiftKey) {
            Object.defineProperty(e, "shiftKey", { value: true });
          }
        }) as EventListener, true);
      }
    }

    if (xtermEl && isTouchDevice) {
      xtermEl.addEventListener("touchstart", () => { term.focus(); }, { passive: true });
    }

    // --- RESTORED: Legacy Selection Buffer ---
    let isSelecting = false;
    let outputBuffer: (Uint8Array | string)[] = [];
    let selectionTimeout: ReturnType<typeof setTimeout> | null = null;

    // xterm.js v5's `ITheme` only exposes the 16 base ANSI colors. The 256-color
    // palette (entries 16-255) is hardcoded internally and cannot be remapped via
    // theme. Claude Code emits its question headings as `\x1b[38;5;16m` (pure
    // #000000, invisible on V2 dark) and accent text as `\x1b[38;5;69m` (royal
    // blue #5F87FF, off-brand). Remap both inline to MarvisX brand truecolor
    // before xterm parses. Done in `onData` (single hot path).
    const ANSI_REMAP_256 = /\x1b\[((?:[\d;]*;)?)38;5;(16|69)(?=[m;])/g;
    const REMAP_BY_INDEX: Record<string, string> = {
      "16": "38;2;246;88;28",   // Riddim orange (#F6581C)
      "69": "38;2;48;166;119",  // MarvisX teal (#30A677)
    };
    const ptyDecoder = new TextDecoder("utf-8", { fatal: false });
    function remapClaudePalette(data: Uint8Array): string {
      const text = ptyDecoder.decode(data, { stream: true });
      return text.replace(ANSI_REMAP_256, (_match, prefix: string, index: string) =>
        `\x1b[${prefix}${REMAP_BY_INDEX[index]}`,
      );
    }

    function writeTerminalChunk(data: Uint8Array | string, callback?: () => void) {
      term.write(data, callback);
    }

    function flushBuffer() {
      isSelecting = false;
      if (selectionTimeout) { clearTimeout(selectionTimeout); selectionTimeout = null; }
      for (const chunk of outputBuffer) writeTerminalChunk(chunk);
      outputBuffer = [];
    }

    function writeOrBuffer(data: Uint8Array | string, callback?: () => void) {
      if (isSelecting) {
        outputBuffer.push(data);
      } else {
        writeTerminalChunk(data, callback);
      }
    }

    // Flush the hidden ring into xterm in arrival order. Synchronous (JS is
    // single-threaded → no onData can interleave during the loop); `draining`
    // and the `bytesBuffered > 0` gate above cover the inter-turn window between
    // isActive flipping true and this running. Routes through writeOrBuffer so an
    // active mouse selection is respected. Overflow is left to the forced
    // reconnect (we don't flush a partial ring).
    drainHiddenRingRef.current = () => {
      if (ringOverflowedRef.current || hiddenRingBytesRef.current === 0) return;
      drainingRef.current = true;
      if (isSelecting) flushBuffer();
      const chunks = hiddenRingChunksRef.current;
      const flushedBytes = hiddenRingBytesRef.current;
      hiddenRingChunksRef.current = [];
      hiddenRingBytesRef.current = 0;
      const t0 = performance.now();
      for (const chunk of chunks) writeOrBuffer(chunk);
      drainingRef.current = false;
      recordTerminalDiagnosticEvent("terminal_hidden_ring_flushed", {
        sessionName,
        sessionProvider: sessionProviderRef.current,
        bytes: flushedBytes,
        durationMs: performance.now() - t0,
      });
    };

    const container = containerRef.current;
    container.addEventListener("mousedown", () => {
      isSelecting = true;
      if (selectionTimeout) clearTimeout(selectionTimeout);
      selectionTimeout = setTimeout(flushBuffer, 500);
    });

    // Auto-copy on selection
    term.onSelectionChange(() => {
      const sel = term.getSelection();
      if (sel) navigator.clipboard.writeText(sel).catch(() => {});
    });

    // --- Key handlers ---
    term.attachCustomKeyEventHandler((e) => {
      if (e.type === "keydown" && isSelecting) flushBuffer();

      // Preserve native macOS Option-character input for non-US layouts.
      if (isPrintableOptionInput(e)) {
        e.preventDefault();
        wsRef.current?.sendInput(e.key);
        return false;
      }

      // Cmd+C / Ctrl+C: copy selection
      if ((e.metaKey || e.ctrlKey) && e.key === "c" && term.hasSelection()) {
        e.preventDefault();
        navigator.clipboard.writeText(term.getSelection()).catch(() => {});
        return false;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "a" && e.type === "keydown") {
        e.preventDefault(); term.selectAll(); return false;
      }
      if (e.ctrlKey && e.key === "s") return false;
      if (e.shiftKey && e.key === "Enter") {
        if (e.type === "keydown") ws.sendInput("\x1b\r");
        return false;
      }
      return true;
    });

    // --- Initial command ---
    function sendInitialCommand() {
      if (initialCommandSentRef.current || !initialCommandRef.current) return;
      initialCommandSentRef.current = true;
      watchingForPromptRef.current = false;
      setTimeout(() => {
        wsRef.current?.sendInput(initialCommandRef.current + "\r");
        onInitialCommandSentRef.current?.();
      }, 300);
    }

    // --- WebSocket ---
    const ws = new ReconnectingTerminalWS(sessionName, {
      onData: (data) => {
        // remapClaudePalette runs the stateful streaming TextDecoder; it MUST be
        // called for every frame in arrival order (hidden or visible) so the
        // decoder stays in sync and multibyte chars split across frames are not
        // corrupted at the join.
        const remapped = remapClaudePalette(data);
        // Hidden HOT pane (mounted, isActive=false) parsing PTY output on the JS
        // main thread saturates it (1-5s typing lag on the visible pane). Instead
        // of dropping, buffer the remapped output in a bounded FIFO ring; on
        // return we flush it locally (no WS teardown). `bytesBuffered > 0` and
        // `draining` keep new live frames queued in FIFO until the drain
        // completes, so flush and live never interleave out of order. Overflow →
        // discard ring + fall back to forced snapshot reconnect.
        if (
          !isActiveRef.current ||
          !panelVisibleRef.current ||
          document.hidden ||
          ringOverflowedRef.current ||
          hiddenRingBytesRef.current > 0 ||
          drainingRef.current
        ) {
          if (!ringOverflowedRef.current) {
            if (hiddenRingBytesRef.current + data.byteLength > HIDDEN_RING_CAP_BYTES) {
              ringOverflowedRef.current = true;
              hiddenRingChunksRef.current = [];
              hiddenRingBytesRef.current = 0;
            } else {
              hiddenRingChunksRef.current.push(remapped);
              hiddenRingBytesRef.current += data.byteLength;
            }
          }
          return;
        }
        const activeForMetrics = isActiveForMetrics();
        recordCounterSample("bytes_received_per_sec", data.byteLength, activeForMetrics, {
          sessionName,
        });
        const writeStartedAt = performance.now();
        writeOrBuffer(remapped, () => {
          recordCounterSample("parse_ms", performance.now() - writeStartedAt, activeForMetrics, {
            sessionName,
          });
          recordPendingInputLatency();
          onPtyOutputParsedRef.current?.();
        });
        if (watchingForPromptRef.current && !initialCommandSentRef.current) {
          if (Date.now() - connectedAtRef.current >= 2000) {
            if (remapped.includes("> ")) sendInitialCommand();
          }
        }
      },
      onStatusChange: (status) => {
        onStatusChangeRef.current?.(status);
        recordTerminalDiagnosticEvent("terminal_ws_status", {
          sessionName,
          sessionProvider: sessionProviderRef.current,
          status,
        });
        if (status === "connected") {
          // Force mouse tracking ON locally (SGR 1006)
          if (termRef.current) {
            termRef.current.write('\x1b[?1000h\x1b[?1006h');
          }
          requestTerminalRecovery({ reason: "ws-connected" });
          if (!initialCommandSentRef.current && initialCommandRef.current) {
            connectedAtRef.current = Date.now();
            watchingForPromptRef.current = true;
            setTimeout(sendInitialCommand, 12000);
          }
        }
      },
      onPing: (message) => {
        if (typeof message.sent_at === "number") {
          const pingAgeMs = Date.now() - message.sent_at * 1000;
          if (Number.isFinite(pingAgeMs) && pingAgeMs >= 0) {
            recordCounterSample("websocket_ping_age_ms", pingAgeMs, isActiveForMetrics(), {
              sessionName,
            });
          }
        }
      },
      onLifecycleEvent: (event: TerminalWSLifecycleEvent) => {
        onLifecycleEventRef.current?.(event);
        recordTerminalDiagnosticEvent("terminal_ws_lifecycle", {
          sessionName,
          sessionProvider: sessionProviderRef.current,
          ...event,
        });
      },
      onAuthError: () => onAuthErrorRef.current?.(),
      getTerminalSize: () => ({ cols: term.cols, rows: term.rows }),
    });
    wsRef.current = ws;

    // Defer connect by one rAF when the initial fit was skipped or returned
    // the safe defaults — gives React + browser layout a frame to settle so
    // the WS query string carries real cols/rows. Otherwise the server creates
    // the tmux pane at the clamped minimum (40 cols) and the first TUI banner
    // is wrapped permanently in tmux scrollback at that wrong width.
    const dimensionsLookDefault = term.cols <= 80 && term.rows <= 24;
    if (!initialContainerReady || dimensionsLookDefault) {
      requestAnimationFrame(() => {
        if (!containerRef.current || !fitAddonRef.current) return;
        const r = containerRef.current.getBoundingClientRect();
        if (r.width > 10 && r.height > 10) {
          fitAddonRef.current.fit();
        }
        ws.connect();
      });
    } else {
      ws.connect();
    }

    const wheelCoalescer = new SgrMouseCoalescer({
      dispatchMs: 16,
      onDispatch: (data) => {
        trackTerminalInput(data);
        ws.sendInput(data);
      },
    });

    const inputDisposable = term.onData((data) => {
      recordWheelInputQueued(data);
      wheelCoalescer.push(data);
    });

    // --- ResizeObserver ---
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width < 2 || height < 2) return;
      scheduleGeometryChangeSync("resize-observer");
    });
    observer.observe(containerRef.current);

    function handleWindowResize() {
      scheduleGeometryChangeSync("window-resize");
    }
    window.addEventListener("resize", handleWindowResize);

    function handleViewportResize() {
      scheduleGeometryChangeSync("visual-viewport-resize");
    }
    window.visualViewport?.addEventListener("resize", handleViewportResize);

    const resizeDisposable = term.onResize(({ cols, rows }) => {
      if (document.hidden || !panelVisibleRef.current || !isActiveRef.current) return;
      scheduleStableResizeSend(cols, rows);
    });

    // --- File upload ---
    let uploadInProgress = false;
    let suppressPasteEventUntil = 0;
    async function doFileUpload(files: File[]) {
      if (uploadInProgress) return;
      uploadInProgress = true;
      recordTerminalDiagnosticEvent("terminal_upload_started", {
        sessionName,
        provider: sessionProviderRef.current,
        files: files.map((file) => ({ name: file.name, type: file.type, size: file.size })),
      });
      try {
        for (const file of files) {
          try {
            const safeFilename = normalizeUploadFilename(
              file.name,
              `upload-${Date.now()}`,
            );
            recordTerminalDiagnosticEvent("browser_file_upload_denied", {
              sessionName,
              provider: sessionProviderRef.current,
              filename: safeFilename,
            });
          } catch (err: unknown) {
            console.error("[upload] failed:", err);
            recordTerminalDiagnosticEvent("terminal_upload_failed", {
              sessionName,
              provider: sessionProviderRef.current,
              error: err instanceof Error ? err.message : String(err),
            });
          }
        }
      } finally { uploadInProgress = false; }
    }

    function handlePaste(e: ClipboardEvent) {
      if (!panelVisibleRef.current || !isActiveRef.current || !e.clipboardData) return;
      const fileItem = Array.from(e.clipboardData.items).find((item) => item.kind === "file");
      if (!fileItem) return;
      recordTerminalDiagnosticEvent("terminal_paste_file_detected", {
        sessionName,
        provider: sessionProviderRef.current,
        itemType: fileItem.type,
      });
      if (Date.now() < suppressPasteEventUntil) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      e.preventDefault();
      e.stopImmediatePropagation();
      const blob = fileItem.getAsFile();
      if (!blob) return;
      const ext = blob.type.split("/")[1] || blob.name?.split(".").pop() || "bin";
      doFileUpload([new File([blob], blob.name || `paste-${Date.now()}.${ext}`, { type: blob.type })]);
    }
    document.addEventListener("paste", handlePaste, true);

    async function handleKeydownPaste(e: KeyboardEvent) {
      if (!panelVisibleRef.current || !isActiveRef.current) return;
      if (!((e.ctrlKey || e.metaKey) && e.key === "v") || !navigator.clipboard?.read) return;
      try {
        for (const item of await navigator.clipboard.read()) {
          const imageType = item.types.find((t) => t.startsWith("image/"));
          if (imageType) {
            recordTerminalDiagnosticEvent("terminal_clipboard_read_image", {
              sessionName,
              provider: sessionProviderRef.current,
              imageType,
            });
            suppressPasteEventUntil = Date.now() + 1000;
            e.preventDefault(); e.stopImmediatePropagation();
            const blob = await item.getType(imageType);
            doFileUpload([new File([blob], `paste-${Date.now()}.${imageType.split("/")[1] || "png"}`, { type: imageType })]); return;
          }
        }
      } catch {
        recordTerminalDiagnosticEvent("terminal_clipboard_read_failed", {
          sessionName,
          provider: sessionProviderRef.current,
        });
      }
    }
    document.addEventListener("keydown", handleKeydownPaste, true);

    function handleDrop(e: DragEvent) {
      e.preventDefault(); e.stopPropagation();
      if (!isActiveRef.current) return;
      const files = Array.from(e.dataTransfer?.files || []);
      recordTerminalDiagnosticEvent("terminal_drop_files", {
        sessionName,
        provider: sessionProviderRef.current,
        count: files.length,
      });
      if (files.length > 0) doFileUpload(files);
    }
    function handleDragOver(e: DragEvent) {
      if (e.dataTransfer?.types.includes("Files")) {
        e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      }
    }
    container.addEventListener("drop", handleDrop);
    container.addEventListener("dragover", handleDragOver);

    function handleTerminalUpload(e: Event) {
      if (!isActiveRef.current) return;
      const files = (e as CustomEvent).detail?.files as File[];
      recordTerminalDiagnosticEvent("terminal_upload_event_received", {
        sessionName,
        provider: sessionProviderRef.current,
        count: files?.length ?? 0,
      });
      if (files?.length > 0) doFileUpload(files);
    }
    window.addEventListener("terminal-upload", handleTerminalUpload);

    function handleWindowBlur() { if (isSelecting) flushBuffer(); }
    window.addEventListener("blur", handleWindowBlur);

    function handleMouseUp() {
      setTimeout(() => { isSelecting = false; flushBuffer(); }, 100);
    }
    document.addEventListener("mouseup", handleMouseUp);

    function handleVisibilityChange() {
      recordTerminalDiagnosticEvent("terminal_document_visibility", {
        sessionName,
        provider: sessionProviderRef.current,
        hidden: document.hidden,
      });
      if (!document.hidden) {
        if (isSelecting) flushBuffer();
        requestTerminalRecovery({
          reason: "active-visible",
          reconnect: true,
        });
      }
    }
    document.addEventListener("visibilitychange", handleVisibilityChange);

    function handleWindowFocus() {
      recordTerminalDiagnosticEvent("terminal_window_focus", {
        sessionName,
        provider: sessionProviderRef.current,
      });
      requestTerminalRecovery({
        reason: "active-visible",
        reconnect: true,
      });
    }
    window.addEventListener("focus", handleWindowFocus);

    // pageshow removed: without `event.persisted` gating, it fired redundantly
    // with mount + focus on every navigation. The bfcache restore case is
    // covered by the focus handler's reconnect probe.

    return () => {
      // Invalidate any in-flight scheduleTerminalSync passes so stale rAFs
      // from the cascade don't run against a disposed term/ws after unmount
      // or sessionName prop change.
      syncRunIdRef.current++;
      inputDisposable.dispose();
      markdownLinkProviderDisposable.dispose();
      wheelCoalescer.dispose();
      resizeDisposable.dispose();
      observer.disconnect();
      document.removeEventListener("keydown", handleKeydownPaste, true);
      document.removeEventListener("paste", handlePaste, true);
      document.removeEventListener("mouseup", handleMouseUp);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      container.removeEventListener("drop", handleDrop);
      container.removeEventListener("dragover", handleDragOver);
      window.removeEventListener("terminal-upload", handleTerminalUpload);
      window.removeEventListener("blur", handleWindowBlur);
      window.removeEventListener("resize", handleWindowResize);
      window.removeEventListener("focus", handleWindowFocus);
      window.visualViewport?.removeEventListener("resize", handleViewportResize);
      recordTerminalDiagnosticEvent("terminal_unmount", {
        ...buildTerminalDiagnosticSnapshot({
          sessionName,
          sessionProvider: sessionProviderRef.current,
          resolvedTheme,
          isActive: isActiveRef.current,
          panelVisible: panelVisibleRef.current,
          term,
          container,
        }),
      });
      cancelScheduledTerminalSync();
      cancelForcedSnapshotReconnect();
      cancelStableResizeSend();
      drainHiddenRingRef.current = null;
      hiddenRingChunksRef.current = [];
      hiddenRingBytesRef.current = 0;
      ringOverflowedRef.current = false;
      drainingRef.current = false;
      ws.close();
      term.dispose();
      termRef.current = null;
      wsRef.current = null;
      fitAddonRef.current = null;
    };
  }, [
    isActiveForMetrics,
    recordWheelInputQueued,
    recordPendingInputLatency,
    cancelForcedSnapshotReconnect,
    cancelStableResizeSend,
    requestTerminalRecovery,
    scheduleGeometryChangeSync,
    scheduleStableResizeSend,
    scheduleTerminalSync,
    sessionName,
    trackTerminalInput,
  ]);

  // Plan A v2 (server-side visibility-gated streaming) was reverted: the
  // `tmux capture-pane` snapshot races our attach-session PTY reader and
  // the kernel-buffered deltas interleave with the replay, garbling the
  // render. Plan B's onData guard below already keeps the xterm parser
  // off the main thread when the pane is hidden — that's the only piece
  // we keep. Cost: hidden panes still pump bytes into the TCP buffer, so
  // the first input after a hidden→visible switch can lag ~p95 4s while
  // the backlog drains. Acceptable vs. garble.

  // resolvedTheme must not recreate the socket/PTY, only repaint the active terminal.
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;

    term.options.theme = terminalTheme;
    recordTerminalDiagnosticEvent("terminal_theme_updated", {
      sessionName,
      provider: sessionProvider,
      resolvedTheme: resolvedTheme ?? null,
      background: terminalBackground,
    });

    if (!isActiveRef.current || !panelVisibleRef.current || document.hidden) return;

    scheduleTerminalSync({ scroll: false, focus: true, reason: "theme-update", force: true });
  }, [resolvedTheme, scheduleTerminalSync, sessionName, sessionProvider, terminalBackground, terminalTheme]);

  // Keep hidden terminals mounted, but only fit the active visible one.
  useEffect(() => {
    if (isActive && panelVisible && termRef.current) {
      recordTerminalDiagnosticEvent("terminal_active_visible", {
        sessionName,
        provider: sessionProvider,
      });
      // Hidden mounted tabs can wake up with a stale socket; always probe reconnect
      // when a terminal becomes the active visible session again. The rAF
      // scrollToBottom for viewport re-attach now lives inside
      // requestTerminalRecovery itself so it covers every callsite (mount,
      // window focus, visibilitychange, ws-connected, theme-update).
      requestTerminalRecovery({ reason: "active-visible", reconnect: true });
    }
  }, [isActive, panelVisible, requestTerminalRecovery, sessionName, sessionProvider]);

  return (
    <div
      className="absolute top-0 right-0 bottom-0 left-3"
      style={{
        visibility: isActive ? "visible" : "hidden",
        pointerEvents: isActive ? "auto" : "none",
        zIndex: isActive ? 1 : 0,
      }}
    >
      <div
        ref={containerRef}
        className="w-full h-full overflow-hidden"
        style={{ backgroundColor: terminalBackground }}
      />
    </div>
  );
}

export default React.memo(TerminalInner);
