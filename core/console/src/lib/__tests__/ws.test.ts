import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReconnectingTerminalWS } from "../ws";

vi.mock("../api", () => ({
  getTerminalTicket: vi.fn().mockResolvedValue({ ticket: "ticket-1" }),
}));

class MockWebSocket {
  static readonly OPEN = 1;
  static readonly instances: MockWebSocket[] = [];

  binaryType = "";
  readyState = MockWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number; reason: string; wasClean: boolean }) => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }
}

describe("ReconnectingTerminalWS", () => {
  beforeEach(() => {
    MockWebSocket.instances.length = 0;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    vi.stubGlobal("WebSocket", MockWebSocket);
  });

  it("bypasses resize dedup when force=true", () => {
    const client = new ReconnectingTerminalWS("test-session", {
      onData: () => {},
      onStatusChange: () => {},
      onAuthError: () => {},
      getTerminalSize: () => ({ cols: 80, rows: 24 }),
    });

    const send = vi.fn();
    (client as unknown as { ws: { readyState: number; send: (payload: Uint8Array) => void } }).ws = {
      readyState: WebSocket.OPEN,
      send,
    };

    client.sendResize(120, 40);
    client.sendResize(120, 40);
    client.sendResize(120, 40, { force: true });

    expect(send).toHaveBeenCalledTimes(2);
  });

  it("forceReconnectForSnapshot closes an open socket, reconnects, and resets resize dedup", async () => {
    const client = new ReconnectingTerminalWS("test-session", {
      onData: () => {},
      onStatusChange: () => {},
      onAuthError: () => {},
      getTerminalSize: () => ({ cols: 120, rows: 40 }),
    });

    await client.connect();
    const firstSocket = MockWebSocket.instances[0];
    firstSocket.onopen?.();

    client.sendResize(120, 40);
    expect(firstSocket.send).toHaveBeenCalledTimes(1);

    client.forceReconnectForSnapshot();

    expect(firstSocket.close).toHaveBeenCalledTimes(1);
    await Promise.resolve();
    await Promise.resolve();
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });

    expect(MockWebSocket.instances).toHaveLength(2);
    const secondSocket = MockWebSocket.instances[1];
    secondSocket.onopen?.();

    client.sendResize(120, 40);
    expect(secondSocket.send).toHaveBeenCalledTimes(1);

    client.close();
  });

  it("dispatches sessions_changed websocket detail to the browser event", async () => {
    const client = new ReconnectingTerminalWS("test-session", {
      onData: () => {},
      onStatusChange: () => {},
      onAuthError: () => {},
      getTerminalSize: () => ({ cols: 80, rows: 24 }),
    });
    const listener = vi.fn();
    window.addEventListener("marvisx:sessions_changed", listener);

    await client.connect();
    MockWebSocket.instances[0].onmessage?.({
      data: JSON.stringify({
        type: "sessions_changed",
        event: "updated",
        session_name: "test-session",
        state: "working",
      }),
    });

    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toMatchObject({
      type: "sessions_changed",
      event: "updated",
      session_name: "test-session",
      state: "working",
    });
    window.removeEventListener("marvisx:sessions_changed", listener);
    client.close();
  });

  it("dispatches session_renamed event with delta payload (Plan 2026-05-21)", async () => {
    const client = new ReconnectingTerminalWS("test-session", {
      onData: () => {},
      onStatusChange: () => {},
      onAuthError: () => {},
      getTerminalSize: () => ({ cols: 80, rows: 24 }),
    });
    const listener = vi.fn();
    window.addEventListener("marvisx:sessions_changed", listener);

    await client.connect();
    MockWebSocket.instances[0].onmessage?.({
      data: JSON.stringify({
        type: "sessions_changed",
        event: "renamed",
        old_name: "old-name",
        new_name: "new-name",
        session_info: {
          name: "new-name",
          prev_name: "old-name",
          provider: "claude",
          model: "claude-opus-4-7",
          project_slug: "marvisx",
          updated_at: "2026-05-21T12:00:00+00:00",
        },
      }),
    });

    expect(listener).toHaveBeenCalledTimes(1);
    const detail = (listener.mock.calls[0][0] as CustomEvent).detail;
    expect(detail).toMatchObject({
      type: "sessions_changed",
      event: "renamed",
      old_name: "old-name",
      new_name: "new-name",
    });
    expect(detail.session_info).toMatchObject({
      name: "new-name",
      prev_name: "old-name",
      provider: "claude",
    });
    window.removeEventListener("marvisx:sessions_changed", listener);
    client.close();
  });

  it("emits connect lifecycle timings for probe, ticket, socket create, and open", async () => {
    const onLifecycleEvent = vi.fn();
    const client = new ReconnectingTerminalWS("test-session", {
      onData: () => {},
      onStatusChange: () => {},
      onAuthError: () => {},
      onLifecycleEvent,
      getTerminalSize: () => ({ cols: 120, rows: 32 }),
    });

    await client.connect();
    MockWebSocket.instances[0].onopen?.();

    const phases = onLifecycleEvent.mock.calls.map(([event]) => event.phase);
    expect(phases).toEqual(expect.arrayContaining([
      "connect_started",
      "direct_probe_completed",
      "ticket_completed",
      "preflight_completed",
      "socket_created",
      "socket_open",
    ]));
    expect(onLifecycleEvent).toHaveBeenCalledWith(expect.objectContaining({
      phase: "socket_created",
      transport: "tunnel",
      cols: 120,
      rows: 32,
    }));
    expect(onLifecycleEvent).toHaveBeenCalledWith(expect.objectContaining({
      phase: "socket_open",
      transport: "tunnel",
      elapsedMs: expect.any(Number),
      openWaitMs: expect.any(Number),
    }));
    client.close();
  });

});
