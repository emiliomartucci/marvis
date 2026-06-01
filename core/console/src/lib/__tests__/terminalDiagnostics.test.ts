import { beforeEach, describe, expect, it } from "vitest";

import {
  clearTerminalDiagnostics,
  getTerminalDiagnosticsTelemetryHealth,
  getTerminalDiagnosticsExport,
  getTerminalDiagnosticsInfo,
  getPendingTerminalDiagnosticsBatch,
  markTerminalDiagnosticsBatchPosted,
  recordCounterSample,
  recordTerminalDiagnosticEvent,
  startTerminalDiagnostics,
} from "../terminalDiagnostics";

describe("terminalDiagnostics counters", () => {
  beforeEach(() => {
    clearTerminalDiagnostics();
  });

  it("summarizes rolling counter samples in diagnostics info and export", () => {
    startTerminalDiagnostics("test");

    recordCounterSample("bytes_received_per_sec", 120, true, {
      sessionName: "demo",
    });
    recordCounterSample("bytes_received_per_sec", 80, true, {
      sessionName: "demo",
    });

    const info = getTerminalDiagnosticsInfo();
    const counter = info.counters.find(
      (item) =>
        item.name === "bytes_received_per_sec" &&
        item.sessionName === "demo" &&
        item.isActive,
    );

    expect(counter?.sum).toBe(200);
    expect(counter?.count).toBe(2);
    expect(getTerminalDiagnosticsExport().counters).toHaveLength(1);
  });

  it("does not record counters when diagnostics are disabled", () => {
    recordCounterSample("parse_ms", 4, true, { sessionName: "demo" });

    expect(getTerminalDiagnosticsInfo().counters).toHaveLength(0);
  });

  it("builds upload batches and remembers posted event ids", () => {
    startTerminalDiagnostics("test");
    recordTerminalDiagnosticEvent("manual_mark", { note: "first" });

    const batch = getPendingTerminalDiagnosticsBatch();

    expect(batch?.events.length).toBeGreaterThan(0);
    expect(batch?.telemetry_health.pendingEvents).toBeGreaterThan(0);
    markTerminalDiagnosticsBatchPosted(batch?.events.map((event) => event.id) ?? []);
    expect(getPendingTerminalDiagnosticsBatch()).toBeNull();
  });

  it("summarizes telemetry health from local events", () => {
    startTerminalDiagnostics("test");
    recordTerminalDiagnosticEvent("terminal_metrics_batch_post_failed", {
      error: "Unauthorized",
      errorKind: "auth",
    });
    recordTerminalDiagnosticEvent("terminal_metrics_fetch_failed", {
      error: "Failed to fetch",
      errorKind: "network",
    });
    recordTerminalDiagnosticEvent("terminal_network_probe_failed", {
      error: "HTTP 503",
      errorKind: "http",
    });
    recordTerminalDiagnosticEvent("terminal_metrics_batch_posted", {
      eventCount: 2,
    });

    const health = getTerminalDiagnosticsTelemetryHealth();

    expect(health.upload.postedBatches).toBe(1);
    expect(health.upload.authFailures).toBe(1);
    expect(health.metricsFetch.networkFailures).toBe(1);
    expect(health.networkProbe.httpFailures).toBe(1);
    expect(getTerminalDiagnosticsExport().telemetry_health.pendingEvents).toBeGreaterThan(0);
  });

  it("exports HOT/COLD activation events and counters", () => {
    startTerminalDiagnostics("test");

    recordTerminalDiagnosticEvent("terminal_cold_to_hot_started", {
      sessionName: "demo",
      previousState: "cold",
      hotCount: 2,
      coldCount: 18,
    });
    recordTerminalDiagnosticEvent("terminal_cold_to_hot_completed", {
      sessionName: "demo",
      durationMs: 120,
    });
    recordCounterSample("mounted_terminal_count", 2, true, { sessionName: "demo" });
    recordCounterSample("cold_terminal_count", 18, true, { sessionName: "demo" });
    recordCounterSample("wheel_events_pre_coalesce", 12, true, { sessionName: "demo", kind: "sgr" });
    recordCounterSample("wheel_events_post_coalesce", 3, true, { sessionName: "demo", kind: "sgr" });

    const exported = getTerminalDiagnosticsExport();

    expect(exported.events.map((event) => event.type)).toContain("terminal_cold_to_hot_started");
    expect(exported.events.map((event) => event.type)).toContain("terminal_cold_to_hot_completed");
    expect(exported.counters.map((counter) => counter.name)).toEqual(
      expect.arrayContaining([
        "mounted_terminal_count",
        "cold_terminal_count",
        "wheel_events_pre_coalesce",
        "wheel_events_post_coalesce",
      ]),
    );
  });
});
