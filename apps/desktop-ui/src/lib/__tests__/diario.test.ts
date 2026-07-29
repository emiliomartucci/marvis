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
        eventIds: [],
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


describe("findTriggeredManualRun (gh #26)", () => {
  // Use the helpers already in the file scope above to build runs.
  function manualRun(opts: {
    runId: string;
    startedAt: string;
    cycleKey?: string;
    status?: BrainRunResponse["status"];
  }): BrainRunResponse {
    return {
      ...run(opts.cycleKey ?? "2026-06-12", opts.status ?? "running"),
      run_id: opts.runId,
      started_at: opts.startedAt,
      trigger: "manual",
    };
  }

  it("returns null when no manual run started after the click timestamp", async () => {
    const { findTriggeredManualRun } = await import("@/lib/diario");
    const trigger = "2026-06-13T10:00:00Z";
    const runs = [
      manualRun({ runId: "old", startedAt: "2026-06-13T09:59:00Z" }),
      // a batch run AFTER the trigger must be ignored
      { ...run("2026-06-12"), started_at: "2026-06-13T10:00:30Z" },
    ];
    expect(findTriggeredManualRun(runs, trigger)).toBeNull();
  });

  it("returns the earliest manual run with started_at >= the click", async () => {
    const { findTriggeredManualRun } = await import("@/lib/diario");
    const trigger = "2026-06-13T10:00:00Z";
    const earliest = manualRun({
      runId: "click",
      startedAt: "2026-06-13T10:00:01Z",
      cycleKey: "2026-06-12",
      status: "succeeded",
    });
    const later = manualRun({
      runId: "next-click",
      startedAt: "2026-06-13T10:01:00Z",
      cycleKey: "2026-06-12",
    });
    const result = findTriggeredManualRun([later, earliest], trigger);
    expect(result?.run_id).toBe("click");
  });

  it("treats started_at == trigger as the run we just launched", async () => {
    const { findTriggeredManualRun } = await import("@/lib/diario");
    const trigger = "2026-06-13T10:00:00Z";
    const same = manualRun({ runId: "exact", startedAt: trigger });
    expect(findTriggeredManualRun([same], trigger)?.run_id).toBe("exact");
  });
});

describe("isTerminalBrainStatus (gh #26)", () => {
  it("treats succeeded / partial / failed / superseded as terminal", async () => {
    const { isTerminalBrainStatus } = await import("@/lib/diario");
    expect(isTerminalBrainStatus("succeeded")).toBe(true);
    expect(isTerminalBrainStatus("partial")).toBe(true);
    expect(isTerminalBrainStatus("failed")).toBe(true);
    expect(isTerminalBrainStatus("superseded")).toBe(true);
  });

  it("keeps running as the only non-terminal status", async () => {
    const { isTerminalBrainStatus } = await import("@/lib/diario");
    expect(isTerminalBrainStatus("running")).toBe(false);
  });
});

describe("hydrateDiaryDay (gh #27)", () => {
  function bodyWithEventIds(): BrainJournalEntryResponse {
    return {
      entry_id: "entry-1",
      run_id: "run-1",
      workspace_id: "ws",
      cycle_key: "2026-06-12",
      scope_type: "company",
      scope_key: "__company__",
      body: {
        what_changed: [
          { domain: "task", event_ids: ["evt-1", "evt-2"] },
        ],
        decisions_observed: [],
        open_loops: [
          { domain: "decision", event_ids: ["evt-3"] },
        ],
        notable_context: [
          { domain: "context", event_ids: ["evt-4"] },
        ],
        sources: [],
        tomorrow_watch: [],
      },
      is_empty: false,
      published_at: "2026-06-12T22:00:00Z",
    };
  }

  it("replaces fallback titles with the event title and fills the project from source_project", async () => {
    const { hydrateDiaryDay, normalizeJournalEntry } = await import("@/lib/diario");
    const day = normalizeJournalEntry(bodyWithEventIds());
    expect(day.progressGroups[0].items[0].text).toMatch(/senza titolo/i);
    expect(day.projectsTouched).toEqual([]);

    const hydrated = hydrateDiaryDay(day, [
      {
        event_id: "evt-1",
        title: "Mappare il ciclo ordini Operations",
        source_project: "casa-lorenzi",
        target_project: null,
      },
      {
        event_id: "evt-3",
        title: "Decidere se rilanciare il contratto",
        source_project: "marvis",
        target_project: null,
      },
      {
        event_id: "evt-4",
        title: "Note brand voice",
        source_project: "casa-lorenzi",
        target_project: null,
      },
    ]);

    expect(hydrated.progressGroups[0].items[0].text).toBe("Mappare il ciclo ordini Operations");
    expect(hydrated.progressGroups[0].items[0].project).toBe("casa-lorenzi");
    expect(hydrated.decisions[0].text).toBe("Decidere se rilanciare il contratto");
    expect(hydrated.decisions[0].project).toBe("marvis");
    expect(hydrated.context[0].text).toBe("Note brand voice");
    expect(hydrated.projectsTouched).toEqual(["casa-lorenzi", "marvis"]);
  });

  it("falls back to target_project when source_project is missing", async () => {
    const { hydrateDiaryDay, normalizeJournalEntry } = await import("@/lib/diario");
    const day = normalizeJournalEntry(bodyWithEventIds());
    const hydrated = hydrateDiaryDay(day, [
      {
        event_id: "evt-1",
        title: "Spostato il task",
        source_project: null,
        target_project: "casa-lorenzi",
      },
    ]);
    expect(hydrated.progressGroups[0].items[0].project).toBe("casa-lorenzi");
  });

  it("does not overwrite a title the body already had inline (LLM polish path)", async () => {
    const { hydrateDiaryDay, normalizeJournalEntry } = await import("@/lib/diario");
    const polishedBody = bodyWithEventIds();
    polishedBody.body.what_changed = [
      { domain: "task", event_ids: ["evt-1"], title: "Titolo lucidato dall'LLM", project: "marvis" },
    ];
    const day = normalizeJournalEntry(polishedBody);
    const hydrated = hydrateDiaryDay(day, [
      { event_id: "evt-1", title: "Titolo grezzo dell'evento", source_project: "casa-lorenzi", target_project: null },
    ]);
    expect(hydrated.progressGroups[0].items[0].text).toBe("Titolo lucidato dall'LLM");
    // project, on the other hand, was missing → fills it
    expect(hydrated.progressGroups[0].items[0].project).toBe("marvis");
  });

  it("returns the day unchanged when no events are provided", async () => {
    const { hydrateDiaryDay, normalizeJournalEntry } = await import("@/lib/diario");
    const day = normalizeJournalEntry(bodyWithEventIds());
    expect(hydrateDiaryDay(day, [])).toBe(day);
  });
});
