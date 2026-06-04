import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { IngestPendingItem, IngestPendingStatus } from "@/lib/types";
import { ACTIVE_INGEST_STATUSES, PendingList } from "../PendingList";

function ingestItem(
  id: string,
  status: IngestPendingStatus,
  filename: string
): IngestPendingItem {
  return {
    id,
    file_path: `/data/projects/marvisx/input/${filename}`,
    project_slug: "marvisx",
    source_kind: "manual_upload",
    mime_type: "application/pdf",
    file_size_bytes: 1024,
    parser_used: "tier-docparse",
    extracted_text: "",
    structure: null,
    classification: null,
    status,
    error_message: null,
    target_folder: null,
    target_filename: null,
    created_at: "2026-05-13 10:00:00",
    updated_at: "2026-05-13 10:00:00",
  };
}

describe("PendingList", () => {
  it("keeps terminal rows out of the active ingest queue", () => {
    render(
      <PendingList
        items={[
          ingestItem("queued-1", "queued", "live-upload.pdf"),
          ingestItem("done-1", "done", "archived-result.pdf"),
          ingestItem("rejected-1", "rejected", "rejected-upload.pdf"),
        ]}
        selectedId={null}
        onSelect={vi.fn()}
        onRefresh={vi.fn()}
      />
    );

    expect(ACTIVE_INGEST_STATUSES).not.toContain("done");
    expect(ACTIVE_INGEST_STATUSES).not.toContain("rejected");
    expect(screen.getByText("live-upload.pdf")).toBeInTheDocument();
    expect(screen.queryByText("archived-result.pdf")).not.toBeInTheDocument();
    expect(screen.queryByText("rejected-upload.pdf")).not.toBeInTheDocument();
    expect(screen.queryByText("Done")).not.toBeInTheDocument();
    expect(screen.queryByText("Rejected")).not.toBeInTheDocument();
  });

  it("supports row and group selection callbacks", async () => {
    const user = userEvent.setup();
    const onToggleSelection = vi.fn();
    const onToggleGroupSelection = vi.fn();

    render(
      <PendingList
        items={[
          ingestItem("queued-1", "queued", "live-upload.pdf"),
          ingestItem("queued-2", "queued", "second-upload.pdf"),
        ]}
        selectedId={null}
        selectedIds={new Set(["queued-1"])}
        onSelect={vi.fn()}
        onToggleSelection={onToggleSelection}
        onToggleGroupSelection={onToggleGroupSelection}
        onRefresh={vi.fn()}
      />
    );

    await user.click(screen.getByLabelText("Seleziona second-upload.pdf"));
    expect(onToggleSelection).toHaveBeenCalledWith("queued-2", true);

    await user.click(screen.getByLabelText("Seleziona gruppo Queued"));
    expect(onToggleGroupSelection).toHaveBeenCalledWith(
      ["queued-1", "queued-2"],
      true
    );
  });
});
