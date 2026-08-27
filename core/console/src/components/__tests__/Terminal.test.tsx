import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";

const moduleMocks = vi.hoisted(() => ({
  recordCounterSample: vi.fn(),
  recordTerminalDiagnosticEvent: vi.fn(),
  mockUseTheme: vi.fn(),
}));

const terminalState = vi.hoisted(() => {
  type MockFn = ReturnType<typeof vi.fn>;
  type MockBufferLine = {
    translateToString: ReturnType<typeof vi.fn<(trimRight?: boolean) => string>>;
  };
  type MockBuffer = {
    length: number;
    baseY: number;
    cursorY: number;
    getLine: ReturnType<typeof vi.fn<(index: number) => MockBufferLine | undefined>>;
  };
  type MockTerminalLink = {
    range: {
      start: { x: number; y: number };
      end: { x: number; y: number };
    };
    text: string;
    activate: (event: MouseEvent, text: string) => void;
  };
  type MockLinkProvider = {
    provideLinks: (
      bufferLineNumber: number,
      callback: (links: MockTerminalLink[] | undefined) => void,
    ) => void;
  };
  type MockTerminalLinkHandler = {
    allowNonHttpProtocols?: boolean;
    activate: (
      event: MouseEvent,
      text: string,
      range: MockTerminalLink["range"],
    ) => void;
  };
  type MockTerminalInstance = {
    setBufferLines: (lines: string[]) => void;
    emitResize: (size: { cols: number; rows: number }) => void;
    write: MockFn;
    linkProviders: MockLinkProvider[];
    registerLinkProvider: MockFn;
    options: { theme?: unknown; scrollback?: number; linkHandler?: MockTerminalLinkHandler };
  };
  type MockTerminalWSHandlers = Record<string, (...args: unknown[]) => void>;
  type MockTerminalWSInstance = {
    handlers: MockTerminalWSHandlers;
    reconnectIfNeeded: MockFn;
    forceReconnectForSnapshot: MockFn;
    sendResize: MockFn;
  };

  const state: {
    terminals: MockTerminalInstance[];
    sockets: MockTerminalWSInstance[];
    MockXTerm?: new () => unknown;
    MockFitAddon?: new () => unknown;
    MockTerminalWS?: new (sessionName: string, handlers: MockTerminalWSHandlers) => unknown;
    setResizeObserverCallback: (callback: ResizeObserverCallback) => void;
    reset: () => void;
  } = {
    terminals: [],
    sockets: [],
    setResizeObserverCallback: () => {},
    reset: () => {},
  };

  class MockXTerm {
    cols = 80;
    rows = 24;
    element: HTMLElement | null = null;
    options: { theme?: unknown; scrollback?: number; linkHandler?: MockTerminalLinkHandler } = {};
    buffer: { active: MockBuffer } = {
      active: {
        length: 0,
        baseY: 0,
        cursorY: 0,
        getLine: vi.fn(() => undefined),
      },
    };
    parser = { registerOscHandler: vi.fn(() => ({ dispose: vi.fn() })) };
    refresh = vi.fn();
    scrollToBottom = vi.fn();
    focus = vi.fn();
    write = vi.fn((_data: unknown, callback?: () => void) => callback?.());
    selectAll = vi.fn();
    hasSelection = vi.fn(() => false);
    getSelection = vi.fn(() => "");
    dispose = vi.fn();
    loadAddon = vi.fn((addon: { __attachTerminal?: (term: MockXTerm) => void }) => addon.__attachTerminal?.(this));
    attachCustomKeyEventHandler = vi.fn();
    linkProviders: MockLinkProvider[] = [];
    registerLinkProvider = vi.fn((provider: MockLinkProvider) => {
      this.linkProviders.push(provider);
      return { dispose: vi.fn() };
    });

    private resizeHandlers: Array<(size: { cols: number; rows: number }) => void> = [];
    private selectionHandlers: Array<() => void> = [];
    private dataHandlers: Array<(data: string) => void> = [];

    constructor(
      options: { theme?: unknown; scrollback?: number; linkHandler?: MockTerminalLinkHandler } = {},
    ) {
      this.options = options;
      state.terminals.push(this);
    }

    setBufferLines(lines: string[]) {
      this.buffer.active = {
        length: lines.length,
        baseY: Math.max(0, lines.length - 1),
        cursorY: 0,
        getLine: vi.fn((index: number): MockBufferLine | undefined => {
          const line = lines[index];
          if (line === undefined) return undefined;
          return {
            translateToString: vi.fn((trimRight?: boolean): string =>
              trimRight ? line.trimEnd() : line,
            ),
          };
        }),
      };
    }

    open(container: HTMLDivElement) {
      const root = container.ownerDocument.createElement("div");
      root.className = "xterm";

      const screen = container.ownerDocument.createElement("div");
      screen.className = "xterm-screen";
      const viewport = container.ownerDocument.createElement("div");
      viewport.className = "xterm-viewport";
      const rows = container.ownerDocument.createElement("div");
      rows.className = "xterm-rows";

      root.append(screen, viewport, rows);
      container.appendChild(root);
      this.element = root;
    }

    onData(handler: (data: string) => void) {
      this.dataHandlers.push(handler);
      return { dispose: vi.fn() };
    }

    onResize(handler: (size: { cols: number; rows: number }) => void) {
      this.resizeHandlers.push(handler);
      return { dispose: vi.fn() };
    }

    emitResize(size: { cols: number; rows: number }) {
      this.cols = size.cols;
      this.rows = size.rows;
      this.resizeHandlers.forEach((handler) => handler(size));
    }

    onSelectionChange(handler: () => void) {
      this.selectionHandlers.push(handler);
      return { dispose: vi.fn() };
    }

    applyFit(container: HTMLDivElement | null) {
      if (!container) return;
      const rect = container.getBoundingClientRect();
      const nextCols = Math.max(1, Math.floor(rect.width / 10));
      const nextRows = Math.max(1, Math.floor(rect.height / 20));
      const changed = nextCols !== this.cols || nextRows !== this.rows;
      this.cols = nextCols;
      this.rows = nextRows;
      if (changed) {
        this.resizeHandlers.forEach((handler) => handler({ cols: nextCols, rows: nextRows }));
      }
    }
  }

  class MockFitAddon {
    private terminal: MockXTerm | null = null;

    __attachTerminal(term: MockXTerm) {
      this.terminal = term;
    }

    fit() {
      const container = this.terminal?.element?.parentElement as HTMLDivElement | null;
      this.terminal?.applyFit(container);
    }
  }

  class MockTerminalWS {
    connect = vi.fn();
    close = vi.fn();
    sendInput = vi.fn();
    sendResize = vi.fn();
    reconnectIfNeeded = vi.fn();
    forceReconnectForSnapshot = vi.fn();

    constructor(
      public readonly sessionName: string,
      public readonly handlers: MockTerminalWSHandlers,
    ) {
      state.sockets.push(this);
    }
  }

  state.MockXTerm = MockXTerm;
  state.MockFitAddon = MockFitAddon;
  state.MockTerminalWS = MockTerminalWS;
  state.setResizeObserverCallback = () => {};
  state.reset = () => {
    state.terminals.length = 0;
    state.sockets.length = 0;
  };

  return state;
});

vi.mock("next-themes", () => ({
  useTheme: () => moduleMocks.mockUseTheme(),
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: terminalState.MockXTerm,
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: terminalState.MockFitAddon,
}));

vi.mock("@xterm/addon-web-links", () => ({
  WebLinksAddon: class {
    constructor() {}
  },
}));

vi.mock("@/lib/ws", () => ({
  ReconnectingTerminalWS: terminalState.MockTerminalWS,
}));

vi.mock("@/lib/api", () => ({}));

vi.mock("@/lib/terminalDiagnostics", () => ({
  recordCounterSample: moduleMocks.recordCounterSample,
  recordTerminalDiagnosticEvent: moduleMocks.recordTerminalDiagnosticEvent,
}));

import Terminal, { buildOpenCodeAutocompleteQuery } from "../Terminal";

describe("Terminal", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    terminalState.reset();
    moduleMocks.mockUseTheme.mockReturnValue({ resolvedTheme: "dark" });

    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });

    Object.defineProperty(window, "devicePixelRatio", {
      configurable: true,
      value: 1,
      writable: true,
    });

    Object.defineProperty(window, "visualViewport", {
      configurable: true,
      value: {
        scale: 1,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
    });

    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      return window.setTimeout(() => callback(0), 0);
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      clearTimeout(id);
    });

    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          terminalState.setResizeObserverCallback(callback);
        }

        observe() {}
        disconnect() {}
      },
    );
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function setTerminalContainerRect(element: HTMLElement, width: number, height: number) {
    Object.defineProperty(element, "getBoundingClientRect", {
      configurable: true,
      value: () => ({
        width,
        height,
        top: 0,
        left: 0,
        right: width,
        bottom: height,
        x: 0,
        y: 0,
        toJSON: () => null,
      }),
    });
  }

  async function flushSyncCycle() {
    await act(async () => {
      vi.runAllTimers();
    });
  }

  type TerminalLinkForTest = {
    range: {
      start: { x: number; y: number };
      end: { x: number; y: number };
    };
    text: string;
    activate: (event: MouseEvent, text: string) => void;
  };

  function collectTerminalLinks(
    provider: {
      provideLinks: (
        bufferLineNumber: number,
        callback: (links: TerminalLinkForTest[] | undefined) => void,
      ) => void;
    },
    bufferLineNumber = 1,
  ) {
    return new Promise<TerminalLinkForTest[] | undefined>((resolve) => {
      provider.provideLinks(bufferLineNumber, resolve);
    });
  }

  it("builds OpenCode upload refs from the stable terminal attachment path", () => {
    expect(
      buildOpenCodeAutocompleteQuery({
        path: "/data/projects/marvisx/attachments/terminal/Session42/20260519-image.png",
        filename: "20260519-image.png",
        size: 12,
        project: "marvisx",
        project_relative_path: "attachments/terminal/Session42/20260519-image.png",
      }),
    ).toBe(
      "data_projects_link/marvisx/attachments/terminal/Session42/20260519-image.png",
    );

    expect(
      buildOpenCodeAutocompleteQuery({
        path: "/data/projects/marvisx/input/20260519-image.png",
        filename: "20260519-image.png",
        size: 12,
        project: "marvisx",
      }),
    ).toBe("data_projects_link/marvisx/input/20260519-image.png");
  });

  it("keeps focus and visibility recovery bounded for OpenCode", async () => {
    const { container } = render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="opencode"
        isActive
        panelVisible
      />,
    );

    const terminalContainer = container.querySelector(".w-full.h-full.overflow-hidden") as HTMLDivElement;
    setTerminalContainerRect(terminalContainer, 800, 400);

    await flushSyncCycle();
    moduleMocks.recordTerminalDiagnosticEvent.mockClear();

    await act(async () => {
      // pageshow handler was removed in F1 — now covered by focus recovery.
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("focus"));
      vi.runAllTimers();
    });

    const applied = moduleMocks.recordTerminalDiagnosticEvent.mock.calls.filter(
      ([type]) => type === "terminal_sync_applied",
    );
    // 4-pass cascade (immediate + settle 75ms + recovery 300ms + 600ms) fires
    // only for the final schedule after dedup via cancelScheduledTerminalSync.
    // Post-c1638cb (restore multi-delay) the bounded count is 4, not 2.
    expect(applied).toHaveLength(4);
    expect(applied.every(([, payload]) => payload.reason === "active-visible")).toBe(true);

    const socket = terminalState.sockets[0];
    // reconnectIfNeeded fires once per schedule (phase=immediate only). Two
    // events fire two schedules; the second cancels the first but both hit
    // the immediate rAF before being superseded under the mocked rAF-as-setTimeout.
    expect(socket.reconnectIfNeeded).toHaveBeenCalledTimes(2);
  });

  it("caps xterm scrollback at 10k lines", async () => {
    render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="claude"
        isActive
        panelVisible
      />,
    );

    expect(terminalState.terminals[0].options.scrollback).toBe(10000);
    await flushSyncCycle();
  });

  it("opens markdown-style terminal links from their labels", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="claude"
        isActive
        panelVisible
      />,
    );

    const terminal = terminalState.terminals[0];
    terminal.setBufferLines([
      'Open [Google](google.com) or "Docs"[docs.example.com/path]',
      "Ignore [Unsafe](javascript:alert(1))",
    ]);

    const provider = terminal.linkProviders[0];
    const links = await collectTerminalLinks(provider);
    expect(links?.map((link) => link.text)).toEqual(["Google", "Docs"]);
    expect(links?.[0].range).toEqual({
      start: { x: 7, y: 1 },
      end: { x: 12, y: 1 },
    });

    const googleLink = links?.[0];
    const docsLink = links?.[1];
    if (!googleLink || !docsLink) throw new Error("Expected markdown terminal links");
    googleLink.activate(new MouseEvent("click"), googleLink.text);
    docsLink.activate(new MouseEvent("click"), docsLink.text);

    expect(openSpy).toHaveBeenNthCalledWith(1, "https://google.com", "_blank", "noopener,noreferrer");
    expect(openSpy).toHaveBeenNthCalledWith(
      2,
      "https://docs.example.com/path",
      "_blank",
      "noopener,noreferrer",
    );

    await expect(collectTerminalLinks(provider, 2)).resolves.toBeUndefined();
    openSpy.mockRestore();
    await flushSyncCycle();
  });

  it("opens OSC terminal hyperlinks through the safe link handler", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="claude"
        isActive
        panelVisible
      />,
    );

    const linkHandler = terminalState.terminals[0].options.linkHandler;
    const range = {
      start: { x: 1, y: 1 },
      end: { x: 4, y: 1 },
    };
    expect(linkHandler?.allowNonHttpProtocols).toBe(true);

    linkHandler?.activate(new MouseEvent("click"), "google.com", range);
    linkHandler?.activate(new MouseEvent("click"), "localhost:3000/status", range);
    linkHandler?.activate(new MouseEvent("click"), "javascript:alert(1)", range);

    expect(openSpy).toHaveBeenCalledTimes(2);
    expect(openSpy).toHaveBeenNthCalledWith(1, "https://google.com", "_blank", "noopener,noreferrer");
    expect(openSpy).toHaveBeenNthCalledWith(
      2,
      "http://localhost:3000/status",
      "_blank",
      "noopener,noreferrer",
    );

    openSpy.mockRestore();
    await flushSyncCycle();
  });

  it("re-syncs once when the geometry fingerprint changes and skips duplicates", async () => {
    const { container } = render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="opencode"
        isActive
        panelVisible
      />,
    );

    const terminalContainer = container.querySelector(".w-full.h-full.overflow-hidden") as HTMLDivElement;
    setTerminalContainerRect(terminalContainer, 800, 400);

    await flushSyncCycle();
    moduleMocks.recordTerminalDiagnosticEvent.mockClear();
    terminalState.sockets[0].sendResize.mockClear();

    Object.defineProperty(window, "devicePixelRatio", {
      configurable: true,
      value: 1.25,
      writable: true,
    });

    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      vi.runAllTimers();
    });

    const appliedAfterFingerprintChange = moduleMocks.recordTerminalDiagnosticEvent.mock.calls.filter(
      ([type, payload]) => type === "terminal_sync_applied" && payload.reason === "geometry-changed",
    );
    expect(appliedAfterFingerprintChange).toHaveLength(1);
    expect(appliedAfterFingerprintChange[0][1].devicePixelRatio).toBe(1.25);
    expect(terminalState.sockets[0].sendResize).toHaveBeenCalledWith(80, 20, { force: true });

    moduleMocks.recordTerminalDiagnosticEvent.mockClear();
    terminalState.sockets[0].sendResize.mockClear();

    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      vi.runAllTimers();
    });

    expect(
      moduleMocks.recordTerminalDiagnosticEvent.mock.calls.some(([type]) => type === "terminal_sync_applied"),
    ).toBe(false);
    expect(terminalState.sockets[0].sendResize).not.toHaveBeenCalled();
  });

  it("debounces transient terminal resizes before sending them to tmux", async () => {
    const { container } = render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="opencode"
        isActive
        panelVisible
      />,
    );

    const terminalContainer = container.querySelector(".w-full.h-full.overflow-hidden") as HTMLDivElement;
    setTerminalContainerRect(terminalContainer, 1480, 600);

    await flushSyncCycle();
    terminalState.sockets[0].sendResize.mockClear();

    await act(async () => {
      terminalState.terminals[0].emitResize({ cols: 148, rows: 30 });
      vi.advanceTimersByTime(100);
      terminalState.terminals[0].emitResize({ cols: 131, rows: 26 });
      vi.advanceTimersByTime(100);
      terminalState.terminals[0].emitResize({ cols: 148, rows: 30 });
      vi.advanceTimersByTime(399);
    });

    expect(terminalState.sockets[0].sendResize).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(1);
    });

    expect(terminalState.sockets[0].sendResize).toHaveBeenCalledTimes(1);
    expect(terminalState.sockets[0].sendResize).toHaveBeenCalledWith(148, 30);
  });

  it("records output bytes and parser duration", async () => {
    render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="claude"
        isActive
        panelVisible
      />,
    );

    await flushSyncCycle();
    moduleMocks.recordCounterSample.mockClear();

    terminalState.sockets[0].handlers.onData(new Uint8Array([65, 66, 67]));

    expect(moduleMocks.recordCounterSample).toHaveBeenCalledWith(
      "bytes_received_per_sec",
      3,
      true,
      { sessionName: "GetMarvisXBetter" },
    );
    expect(moduleMocks.recordCounterSample).toHaveBeenCalledWith(
      "parse_ms",
      expect.any(Number),
      true,
      { sessionName: "GetMarvisXBetter" },
    );
  });

  it("drops PTY frames and records diagnostic when pane is hidden (Plan 2026-05-25)", async () => {
    render(
      <Terminal
        sessionName="HiddenPane"
        sessionProvider="claude"
        isActive={false}
        panelVisible
      />,
    );

    await flushSyncCycle();
    moduleMocks.recordCounterSample.mockClear();
    moduleMocks.recordTerminalDiagnosticEvent.mockClear();

    terminalState.sockets[0].handlers.onData(new Uint8Array([72, 73, 74, 75, 76]));

    expect(moduleMocks.recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
      "terminal_hidden_frame_dropped",
      expect.objectContaining({
        sessionName: "HiddenPane",
        sessionProvider: "claude",
        bytes: 5,
      }),
    );
    expect(moduleMocks.recordCounterSample).not.toHaveBeenCalledWith(
      "bytes_received_per_sec",
      expect.any(Number),
      expect.any(Boolean),
      expect.anything(),
    );
    expect(moduleMocks.recordCounterSample).not.toHaveBeenCalledWith(
      "parse_ms",
      expect.any(Number),
      expect.any(Boolean),
      expect.anything(),
    );
  });

  it("forces a snapshot reconnect when a HOT pane dropped frames while inactive", async () => {
    const { rerender } = render(
      <Terminal
        sessionName="HiddenPane"
        sessionProvider="claude"
        isActive={false}
        panelVisible
      />,
    );

    await flushSyncCycle();
    terminalState.sockets[0].reconnectIfNeeded.mockClear();
    terminalState.sockets[0].forceReconnectForSnapshot.mockClear();

    terminalState.sockets[0].handlers.onData(new Uint8Array([72, 73, 74]));

    rerender(
      <Terminal
        sessionName="HiddenPane"
        sessionProvider="claude"
        isActive
        panelVisible
      />,
    );

    await act(async () => {
      vi.advanceTimersByTime(249);
    });
    expect(terminalState.sockets[0].forceReconnectForSnapshot).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(1);
    });

    expect(terminalState.sockets[0].forceReconnectForSnapshot).toHaveBeenCalledTimes(1);
    expect(terminalState.sockets[0].reconnectIfNeeded).not.toHaveBeenCalled();
    await flushSyncCycle();
  });

  it("drops PTY frames when panelVisible is false even if isActive", async () => {
    render(
      <Terminal
        sessionName="PanelHidden"
        sessionProvider="claude"
        isActive
        panelVisible={false}
      />,
    );

    await flushSyncCycle();
    moduleMocks.recordCounterSample.mockClear();
    moduleMocks.recordTerminalDiagnosticEvent.mockClear();

    terminalState.sockets[0].handlers.onData(new Uint8Array([88, 89]));

    expect(moduleMocks.recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
      "terminal_hidden_frame_dropped",
      expect.objectContaining({ sessionName: "PanelHidden", bytes: 2 }),
    );
    expect(moduleMocks.recordCounterSample).not.toHaveBeenCalledWith(
      "bytes_received_per_sec",
      expect.any(Number),
      expect.any(Boolean),
      expect.anything(),
    );
  });

  it("records websocket lifecycle diagnostics with session context", async () => {
    const onLifecycleEvent = vi.fn();
    render(
      <Terminal
        sessionName="GetMarvisXBetter"
        sessionProvider="claude"
        isActive
        panelVisible
        onLifecycleEvent={onLifecycleEvent}
      />,
    );

    const lifecycleEvent = {
      phase: "socket_open",
      attempt: 0,
      elapsedMs: 123,
      openWaitMs: 45,
      transport: "tunnel",
    } as const;
    terminalState.sockets[0].handlers.onLifecycleEvent(lifecycleEvent);

    expect(onLifecycleEvent).toHaveBeenCalledWith(lifecycleEvent);
    expect(moduleMocks.recordTerminalDiagnosticEvent).toHaveBeenCalledWith(
      "terminal_ws_lifecycle",
      expect.objectContaining({
        sessionName: "GetMarvisXBetter",
        sessionProvider: "claude",
        phase: "socket_open",
        elapsedMs: 123,
        openWaitMs: 45,
        transport: "tunnel",
      }),
    );
    await flushSyncCycle();
  });

});
