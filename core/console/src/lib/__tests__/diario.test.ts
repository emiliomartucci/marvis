import { afterEach, describe, expect, it, vi } from "vitest";

import { APIError } from "@/lib/api";
import {
  buildTimelineDays,
  deriveTimelineState,
  normalizeJournalEntry,
  requestBrainRunNow,
  selectDiaryLimitState,
  timelineStateClasses,
  type NormalizedDiaryDay,
} from "@/lib/diario";
import type { BrainJournalEntryResponse, BrainRunResponse } from "@/lib/api";

function run(cycleKey: string, status: BrainRunResponse["status"] = "succeeded"): BrainRunResponse {
  return {
    run_id: `run-${cycleKey}`,
    workspace_id: "ws_default",
    cycle_key: cycleKey,
    cycle_window_start_utc: `${cycleKey}T00:00:00Z`,
    cycle_window_end_utc: `${cycleKey}T23:59:59Z`,
    cutoff_hour_utc_at_run: 3,
    scope_type: "company",
    scope_key: "__company__",
    trigger: "batch",
    started_at: `${cycleKey}T03:00:00Z`,
    finished_at: `${cycleKey}T03:10:00Z`,
    status,
    superseded_by_run_id: null,
    event_count: 2,
    partial_failures: [],
    duration_ms: 600000,
    error_summary: null,
  };
}

function journal(overrides: Partial<BrainJournalEntryResponse> = {}): BrainJournalEntryResponse {
  return {
    entry_id: "journal-1",
    run_id: "run-2026-06-12",
    workspace_id: "ws_default",
    cycle_key: "2026-06-12",
    scope_type: "company",
    scope_key: "__company__",
    program_key: null,
    body: {
      what_changed: [
        { text: "Shipping notes updated", project: "marvisx" },
      ],
      decisions_observed: ["ADR accepted"],
      open_loops: [
        { text: "Decide launch order", project: "marvisx", source_ref: "brain:loop:1" },
      ],
      notable_context: [
        { title: "Mirko call context", project: "demo" },
      ],
      sources: ["event-1"],
      tomorrow_watch: [{ text: "Watch onboarding" }],
    },
    is_empty: false,
    published_at: "2026-06-12T03:15:00Z",
    redacted_count: 0,
    narrative_polished: "The brain found momentum. One decision needs Emilio.",
    cited_evidence_refs: [],
    polish_model: "test",
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("diario mapping", () => {
  it("maps the journal fixture into the ordered diary sections", () => {
    const mapped = normalizeJournalEntry(journal());

    expect(mapped.narrative).toContain("momentum");
    expect(mapped.decisions).toEqual([
      {
        id: "journal-1:open:0",
        text: "Decide launch order",
        project: "marvisx",
        sourceRef: "brain:loop:1",
      },
    ]);
    expect(mapped.progressGroups).toHaveLength(2);
    expect(mapped.progressGroups.flatMap((group) => group.items).map((item) => item.text)).toEqual([
      "Shipping notes updated",
      "ADR accepted",
    ]);
    expect(mapped.context.map((item) => item.text)).toEqual(["Mirko call context"]);
    expect(mapped.projectsTouched).toEqual(["demo", "marvisx"]);
    expect(mapped.counts).toMatchObject({
      decisions: 1,
      progress: 2,
      context: 1,
      sources: 1,
      tomorrowWatch: 1,
    });
  });

  it("falls back to a base summary when polished narrative is missing", () => {
    const mapped = normalizeJournalEntry(journal({ narrative_polished: null }));

    expect(mapped.narrative).toBeNull();
    expect(mapped.baseSummary).toBe("Shipping notes updated");
    expect(mapped.narrativeFallback).toBe(true);
  });
});

describe("diario limit states and timeline states", () => {
  const activeJournal = normalizeJournalEntry(journal());
  const emptyJournal: NormalizedDiaryDay = {
    ...activeJournal,
    isEmpty: true,
    decisions: [],
    context: [],
    progressGroups: [],
  };

  it("separates ran-empty from not-ran", () => {
    expect(selectDiaryLimitState({ cycleKey: "2026-06-12", run: run("2026-06-12"), journal: emptyJournal, state: "quiet" })).toBe("quiet");
    expect(selectDiaryLimitState({ cycleKey: "2026-06-11", run: null, journal: null, state: "not_run" })).toBe("not_run");
    expect(selectDiaryLimitState({ cycleKey: "2026-06-12", run: run("2026-06-12"), journal: activeJournal, state: "needs_decision" })).toBe("active");
  });

  it("derives tick color states from runs plus journal data", () => {
    expect(deriveTimelineState(run("2026-06-12"), activeJournal)).toBe("needs_decision");
    expect(deriveTimelineState(run("2026-06-12"), { ...activeJournal, decisions: [] })).toBe("managed");
    expect(deriveTimelineState(run("2026-06-12"), emptyJournal)).toBe("quiet");
    expect(deriveTimelineState(null, null)).toBe("not_run");

    expect(timelineStateClasses("needs_decision", true)).toContain("bg-pir-accent");
    expect(timelineStateClasses("managed", false)).toContain("bg-pir-success");
    expect(timelineStateClasses("quiet", false)).toContain("bg-pir-border-strong");
    expect(timelineStateClasses("not_run", false)).toContain("border-dashed");
  });

  it("fills missing days from today through the oldest run", () => {
    const days = buildTimelineDays(
      [run("2026-06-12"), run("2026-06-10")],
      [activeJournal],
      new Date("2026-06-12T10:00:00"),
    );

    expect(days.map((day) => [day.cycleKey, day.state])).toEqual([
      ["2026-06-12", "needs_decision"],
      ["2026-06-11", "not_run"],
      ["2026-06-10", "quiet"],
    ]);
  });
});

describe("brain run request", () => {
  it("returns started on the 202 path", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ started: true }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(requestBrainRunNow()).resolves.toBe("started");
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/brain/run"),
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("maps the 409 path to already_running", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: { error_kind: "already_running" },
    }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(requestBrainRunNow()).resolves.toBe("already_running");
  });

  it("does not hide unexpected brain run errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "broken" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(requestBrainRunNow()).rejects.toBeInstanceOf(APIError);
  });
});
