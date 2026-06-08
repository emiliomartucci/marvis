import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { IngestHistoryEntry } from "@/lib/types";

const { fetchTriageCounters, listIngestHistory } = vi.hoisted(() => ({
  fetchTriageCounters: vi.fn(),
  listIngestHistory: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  fetchTriageCounters,
  listIngestHistory,
}));

import { AutoApprovedCounter } from "../AutoApprovedCounter";

const historyRows: IngestHistoryEntry[] = [
  {
    id: "auto-1",
    source: "ingest_pending",
    decision: "auto_approved",
    status: "done",
    file_path: "/data/projects/marvisx/input/auto.md",
    filename: "auto.md",
    project_slug: "marvisx",
    mime_type: "text/markdown",
    file_size_bytes: 2048,
    parser_used: "tier-fast",
    document_type: "guide",
    confidence: 0.93,
    target_folder: "docs/guides",
    target_filename: "auto.md",
    reason: "auto_approve:llm_routing",
    triage_decision_id: "auto_approve:llm_routing",
    existing_ingest_id: null,
    created_at: "2026-05-16 10:00:00",
    updated_at: "2026-05-16 10:00:00",
  },
  {
    id: "skip-1",
    source: "ingest_skipped",
    decision: "skipped",
    status: "skipped",
    file_path: "ignored.exe",
    filename: "ignored.exe",
    project_slug: "marvisx",
    mime_type: null,
    file_size_bytes: null,
    parser_used: null,
    document_type: null,
    confidence: null,
    target_folder: null,
    target_filename: null,
    reason: "mime_not_allowed",
    triage_decision_id: null,
    existing_ingest_id: null,
    created_at: "2026-05-16 10:01:00",
    updated_at: "2026-05-16 10:01:00",
  },
];

describe("AutoApprovedCounter", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchTriageCounters.mockResolvedValue({ auto: 3, manual: 2 });
    listIngestHistory.mockResolvedValue(historyRows);
  });

  it("opens a real decision history drawer", async () => {
    const user = userEvent.setup();
    render(<AutoApprovedCounter />);

    await screen.findByRole("button", { name: /3 auto-approvati e 2 manuali oggi/i });
    await user.click(screen.getByRole("button", { name: /auto-approvati/i }));

    const dialog = await screen.findByRole("dialog", {
      name: "Storico decisioni ingest",
    });
    expect(within(dialog).getByText("auto.md")).toBeInTheDocument();
    expect(within(dialog).getByText("ignored.exe")).toBeInTheDocument();
    expect(within(dialog).getByText("AUTO OK")).toBeInTheDocument();
    expect(within(dialog).getByText("IGNORATO")).toBeInTheDocument();
    expect(within(dialog).queryByText(/placeholder/i)).not.toBeInTheDocument();
  });

  it("refetches history when the drawer filter changes", async () => {
    const user = userEvent.setup();
    render(<AutoApprovedCounter />);

    await user.click(await screen.findByRole("button", { name: /auto-approvati/i }));
    await screen.findByText("auto.md");
    await user.click(screen.getByRole("button", { name: "Auto reject" }));

    await waitFor(() => {
      expect(listIngestHistory).toHaveBeenLastCalledWith(
        expect.objectContaining({ decision: "auto_rejected", today: true })
      );
    });
  });
});
