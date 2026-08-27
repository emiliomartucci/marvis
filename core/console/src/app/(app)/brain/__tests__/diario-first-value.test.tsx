import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import BrainDiarioPage from "../diario/page";
import { countDiarioRegistri } from "@/lib/brain/diarioFirstValue";
import { fetchJournal } from "@/lib/brain/surfaces";
import { emitGuiFirstValue } from "@/lib/guiEvents";

const mockBrainContext = vi.hoisted(() => ({
  cycleKey: "latest" as string | null,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/components/brain/useBrainContext", () => ({
  useBrainContext: () => ({ cycleKey: mockBrainContext.cycleKey, scope: "company" }),
}));

vi.mock("@/lib/brain/useEventTitles", () => ({
  useEventTitles: () => ({}),
}));

vi.mock("@/lib/brain/surfaces", () => ({
  fetchJournal: vi.fn(),
}));

vi.mock("@/lib/guiEvents", () => ({
  emitGuiFirstValue: vi.fn(async () => ({
    event_name: "gui_first_value",
    emitted: true,
    event_id: "gui-event-1",
    seen_count: 1,
    first_seen_at: "2026-07-09T06:00:00Z",
  })),
}));

describe("Brain Diario first value tracking", () => {
  let intersectionCallback: IntersectionObserverCallback | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
    mockBrainContext.cycleKey = "latest";
    intersectionCallback = null;
    window.history.pushState({}, "", "/ui/brain/diario/");
    class MockIntersectionObserver implements IntersectionObserver {
      readonly root = null;
      readonly rootMargin = "0px";
      readonly thresholds = [0.1];

      constructor(callback: IntersectionObserverCallback) {
        intersectionCallback = callback;
      }

      disconnect = vi.fn();
      observe = vi.fn();
      takeRecords = vi.fn(() => []);
      unobserve = vi.fn();
    }
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    vi.mocked(fetchJournal).mockResolvedValue({
      items: [{
        entry_id: "entry-visible",
        run_id: "run-visible",
        workspace_id: "ws_default",
        cycle_key: "2026-07-09",
        scope_type: "company",
        scope_key: "__company__",
        body: {
          what_changed: ["event-1"],
          decisions_observed: [],
          open_loops: [],
          notable_context: [],
          sources: ["event-1"],
          tomorrow_watch: [],
        },
        is_empty: false,
        published_at: "2026-07-09T06:00:00Z",
        narrative_polished: "Marvis rendered real hosted value.",
      }],
      total_returned: 1,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("counts rendered Diario registri", () => {
    expect(countDiarioRegistri({
      what_changed: ["a"],
      decisions_observed: ["b"],
      open_loops: [],
      notable_context: ["c"],
      tomorrow_watch: [],
      sources: ["d"],
    }, true)).toBe(5);
  });

  it("emits gui_first_value only after the Diario article enters the viewport", async () => {
    render(<BrainDiarioPage />);

    const article = await screen.findByTestId("brain-diario-first-value");
    await waitFor(() => expect(fetchJournal).toHaveBeenCalled());
    expect(emitGuiFirstValue).not.toHaveBeenCalled();

    intersectionCallback?.([
      { isIntersecting: true, target: article } as IntersectionObserverEntry,
    ], {} as IntersectionObserver);

    await waitFor(() => {
      expect(emitGuiFirstValue).toHaveBeenCalledWith(expect.objectContaining({
        surface: "brain_diario",
        route: "/ui/brain/diario/",
        cycle_key: "2026-07-09",
        run_id: "run-visible",
        entry_id: "entry-visible",
        registri_count: 3,
      }));
    });
  });

  it("emits gui_first_value for Diario entries with only operational registri", async () => {
    vi.mocked(fetchJournal).mockResolvedValueOnce({
      items: [{
        entry_id: "entry-open-loops",
        run_id: "run-open-loops",
        workspace_id: "ws_default",
        cycle_key: "2026-07-09",
        scope_type: "company",
        scope_key: "__company__",
        body: {
          what_changed: [],
          decisions_observed: [],
          open_loops: ["Close CE1 WP3 smoke evidence."],
          notable_context: [],
          sources: [],
          tomorrow_watch: [],
        },
        is_empty: false,
        published_at: "2026-07-09T06:00:00Z",
        narrative_polished: "",
      }],
      total_returned: 1,
    });

    render(<BrainDiarioPage />);

    const article = await screen.findByTestId("brain-diario-first-value");
    intersectionCallback?.([
      { isIntersecting: true, target: article } as IntersectionObserverEntry,
    ], {} as IntersectionObserver);

    await waitFor(() => {
      expect(emitGuiFirstValue).toHaveBeenCalledWith(expect.objectContaining({
        entry_id: "entry-open-loops",
        registri_count: 1,
      }));
    });
  });

  it("clears stale Diario entries when a later reload fails", async () => {
    const { rerender } = render(<BrainDiarioPage />);

    const article = await screen.findByTestId("brain-diario-first-value");
    intersectionCallback?.([
      { isIntersecting: true, target: article } as IntersectionObserverEntry,
    ], {} as IntersectionObserver);
    await waitFor(() => expect(emitGuiFirstValue).toHaveBeenCalledTimes(1));

    vi.mocked(fetchJournal).mockRejectedValueOnce(new Error("journal unavailable"));
    mockBrainContext.cycleKey = "2026-07-10";
    rerender(<BrainDiarioPage />);

    await waitFor(() => {
      expect(screen.queryByTestId("brain-diario-first-value")).toBeNull();
    });
    expect(await screen.findByText("Nessun racconto per questo ciclo")).toBeTruthy();
    expect(emitGuiFirstValue).toHaveBeenCalledTimes(1);
  });
});
