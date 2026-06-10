import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { IngestPendingItem } from "@/lib/types";
import IngestTriagePage from "@/app/(app)/inbox/triage/files/page";
import { FolderUpload } from "@/components/ingest/FolderUpload";
import { PendingDetail } from "@/components/ingest/PendingDetail";

const {
  approveIngestPending,
  fetchAutoApprovedCount,
  fetchTriageCounters,
  getPrograms,
  getIngestPreviewBlob,
  getIngestPreviewMd,
  listIngestHistory,
  listIngestPending,
  listIngestSkipped,
  rejectIngestPending,
  retryParseIngestPending,
  uploadIngestFolder,
  uploadIngestZip,
} = vi.hoisted(() => ({
  approveIngestPending: vi.fn(),
  fetchAutoApprovedCount: vi.fn(),
  fetchTriageCounters: vi.fn(),
  getPrograms: vi.fn(),
  getIngestPreviewBlob: vi.fn(),
  getIngestPreviewMd: vi.fn(),
  listIngestHistory: vi.fn(),
  listIngestPending: vi.fn(),
  listIngestSkipped: vi.fn(),
  rejectIngestPending: vi.fn(),
  retryParseIngestPending: vi.fn(),
  uploadIngestFolder: vi.fn(),
  uploadIngestZip: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  approveIngestPending,
  fetchAutoApprovedCount,
  fetchTriageCounters,
  getPrograms,
  getIngestPreviewBlob,
  getIngestPreviewMd,
  getIngestPreviewUrl: (id: string, ext: string) => `/api/v1/ingest/pending/${id}/preview.${ext}`,
  listIngestHistory,
  listIngestPending,
  listIngestSkipped,
  rejectIngestPending,
  retryParseIngestPending,
  uploadIngestFolder,
  uploadIngestZip,
}));

function mockPendingItems(
  itemsOrFactory: IngestPendingItem[] | (() => IngestPendingItem[])
) {
  listIngestPending.mockImplementation(
    (opts?: { status?: IngestPendingItem["status"] }) => {
      const items =
        typeof itemsOrFactory === "function" ? itemsOrFactory() : itemsOrFactory;
      return Promise.resolve(
        opts?.status ? items.filter((item) => item.status === opts.status) : items
      );
    }
  );
}

describe("Ingest Triage UI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.pushState({}, "", "/inbox/triage/files/");
    fetchAutoApprovedCount.mockResolvedValue(0);
    fetchTriageCounters.mockResolvedValue({ auto: 0, manual: 0 });
    getPrograms.mockResolvedValue([mockProgram]);
    getIngestPreviewBlob.mockResolvedValue(new Blob(["pdf"], { type: "application/pdf" }));
    getIngestPreviewMd.mockResolvedValue("# Preview");
    listIngestHistory.mockResolvedValue([]);
    listIngestSkipped.mockResolvedValue([]);
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:test-pdf"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    approveIngestPending.mockResolvedValue({ id: mockItem.id, status: "approved" });
    rejectIngestPending.mockResolvedValue({ id: mockItem.id, status: "rejected" });
    retryParseIngestPending.mockResolvedValue({ id: mockItem.id, status: "parsing" });
    mockPendingItems([]);
    uploadIngestFolder.mockResolvedValue({
      project_slug: "marvisx",
      uploaded_files: 1,
      queued_items: 1,
      skipped_files: [],
    });
    uploadIngestZip.mockResolvedValue({
      project_slug: "marvisx",
      uploaded_files: 1,
      queued_items: 1,
      skipped_files: [],
    });
  });

  it("FE-P0-1 cross-tab race: same id approved elsewhere fades out via WS event", async () => {
    let pendingItems = [mockItem, otherItem];
    mockPendingItems(() => pendingItems);

    render(<IngestTriagePage />);

    await waitFor(() => expect(screen.getAllByText("test.md").length).toBeGreaterThan(0));
    expect(screen.getAllByText("other.md").length).toBeGreaterThan(0);

    pendingItems = [otherItem];
    await act(async () => {
      window.dispatchEvent(new CustomEvent("marvisx:ingest_changed"));
    });

    await waitFor(() => expect(screen.queryByText("test.md")).not.toBeInTheDocument());
    expect(screen.getAllByText("other.md").length).toBeGreaterThan(0);
  });

  it("FE-P0-2 WS handler registers marvisx:ingest_changed and cleans up", async () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");
    listIngestPending.mockResolvedValue([]);

    const { unmount } = render(<IngestTriagePage />);
    await screen.findByLabelText(/auto-approvati.*oggi/i);

    expect(addSpy).toHaveBeenCalledWith("marvisx:ingest_changed", expect.any(Function));
    unmount();
    expect(removeSpy).toHaveBeenCalledWith("marvisx:ingest_changed", expect.any(Function));
  });

  it("FE-P0-3 state union: approve disables button and shows in-progress label", async () => {
    const pendingApprove = new Promise<void>(() => undefined);

    render(
      <PendingDetail
        item={mockItem}
        previewText="# Preview"
        onApprove={() => pendingApprove}
        onReject={vi.fn()}
        onRetryParse={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("button", { name: "Approvazione..." })).toBeDisabled();
  });

  it("FE-P1-4 MIME re-render: PDF to image does not leave stale iframe", async () => {
    const { rerender } = render(
      <PendingDetail
        item={{ ...mockItem, mime_type: "application/pdf", file_path: "/data/projects/marvisx/input/test.pdf" }}
        previewText=""
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onRetryParse={vi.fn()}
      />
    );

    expect(await screen.findByTitle(/PDF preview/i)).toBeInTheDocument();

    rerender(
      <PendingDetail
        item={{ ...mockItem, mime_type: "image/png", file_path: "/data/projects/marvisx/input/test.png" }}
        previewText=""
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onRetryParse={vi.fn()}
      />
    );

    expect(screen.queryByTitle(/PDF preview/i)).not.toBeInTheDocument();
    expect(screen.getByAltText(/Anteprima test.md/i)).toBeInTheDocument();
  });

  it("FE-P1-5 off-route suspend: ingest WS event on /graph does not refetch", async () => {
    mockPendingItems([mockItem]);

    render(<IngestTriagePage />);
    await waitFor(() => expect(screen.getAllByText("test.md").length).toBeGreaterThan(0));
    listIngestPending.mockClear();
    window.history.pushState({}, "", "/graph/");

    await act(async () => {
      window.dispatchEvent(new CustomEvent("marvisx:ingest_changed"));
    });

    expect(listIngestPending).not.toHaveBeenCalled();
  });

  it("FE-P1-6 auto-approve counter remains visible with zero awaiting items", async () => {
    mockPendingItems([]);
    fetchAutoApprovedCount.mockResolvedValue(0);

    render(<IngestTriagePage />);

    expect(await screen.findByLabelText("0 auto-approvati e 0 manuali oggi")).toBeInTheDocument();
    expect(screen.getByText("L'apparato digerente e' a riposo.")).toBeInTheDocument();
  });

  it("FE-P1-6b transcript preview uses extracted text without markdown fetch", async () => {
    const transcriptItem: IngestPendingItem = {
      ...mockItem,
      id: "00000000-0000-0000-0000-000000000003",
      file_path: "/data/projects/marvisx/input/meeting.m4a",
      mime_type: "audio/x-m4a",
      parser_used: "tier_transcribe",
      extracted_text: "# Transcript\n\nMeeting notes",
      target_folder: "docs/transcripts",
      target_filename: "meeting.md",
      classification: {
        ...classification,
        type: "transcript",
        title: "meeting.md",
        target_folder: "docs/transcripts",
        target_filename: "meeting.md",
      },
    };
    mockPendingItems([transcriptItem]);

    render(<IngestTriagePage />);

    await waitFor(() => expect(screen.getAllByText("meeting.md").length).toBeGreaterThan(0));
    expect(await screen.findByText("Meeting notes")).toBeInTheDocument();
    expect(getIngestPreviewMd).not.toHaveBeenCalled();
  });

  it("FE-P2-7 PDF iframe CSP: files over 20 MB show fallback link", () => {
    render(
      <PendingDetail
        item={{
          ...mockItem,
          mime_type: "application/pdf",
          file_path: "/data/projects/marvisx/input/test.pdf",
          file_size_bytes: 25 * 1024 * 1024,
        }}
        previewText=""
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onRetryParse={vi.fn()}
      />
    );

    expect(screen.getByText("Apri in nuova scheda")).toBeInTheDocument();
    expect(screen.queryByTitle(/PDF preview/i)).not.toBeInTheDocument();
  });

  it("FE-P2-8 theme-v2 conformance: rendered UI has no hardcoded hex colors", async () => {
    mockPendingItems([mockItem]);

    const { container } = render(<IngestTriagePage />);
    await waitFor(() => expect(screen.getAllByText("test.md").length).toBeGreaterThan(0));

    expect(container.innerHTML.match(/#[0-9a-fA-F]{6}/g) ?? []).toHaveLength(0);
  });

  it("FE-P2-9 folder upload control stays on the triage surface", async () => {
    mockPendingItems([mockItem]);

    render(<IngestTriagePage />);
    await waitFor(() => expect(screen.getAllByText("test.md").length).toBeGreaterThan(0));

    expect(screen.getByRole("button", { name: /folder/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /zip/i })).toBeInTheDocument();
  });

  it("FE-P2-10 zip drop uses the zip upload endpoint", async () => {
    const archive = new File(["zip"], "archive.zip", { type: "application/zip" });
    render(<FolderUpload projectSlug="marvisx" onUploaded={vi.fn()} />);

    fireEvent.drop(screen.getByText(/marvisx · ready/i).parentElement!, {
      dataTransfer: {
        files: [archive],
        items: [],
      },
    });

    await waitFor(() => expect(uploadIngestZip).toHaveBeenCalledWith("marvisx", archive));
    expect(uploadIngestFolder).not.toHaveBeenCalled();
  });
});

const classification = {
  type: "handoff",
  title: "test.md",
  tags: ["ingest", "triage"],
  target_folder: "memory",
  target_filename: "test.md",
  confidence: 0.91,
  reason: "Matches handoff frontmatter",
  auto_approve: false,
};

const mockItem: IngestPendingItem = {
  id: "00000000-0000-0000-0000-000000000001",
  file_path: "/data/projects/marvisx/input/test.md",
  project_slug: "marvisx",
  source_kind: "file_drop",
  mime_type: "text/markdown",
  file_size_bytes: 1024,
  parser_used: "internal_markdown",
  extracted_text: "# Preview",
  structure: null,
  classification,
  status: "awaiting_triage",
  error_message: null,
  target_folder: "memory",
  target_filename: "test.md",
  created_at: "2026-04-27T10:00:00Z",
  updated_at: "2026-04-27T10:00:00Z",
};

const otherItem: IngestPendingItem = {
  ...mockItem,
  id: "00000000-0000-0000-0000-000000000002",
  file_path: "/data/projects/marvisx/input/other.md",
  target_filename: "other.md",
  classification: { ...classification, title: "other.md", target_filename: "other.md" },
};

const mockProgram = {
  name: "marvis",
  description: "Marvis program",
  projects: [
    {
      slug: "marvisx",
      name: "marvisx",
      program: "marvis",
      language: "python",
      lifecycle: "active",
      phase: "operativo",
      scope: "personal",
      description: null,
      type: "code",
      repo_path: "/var/marvisx/workspace",
      metadata_path: "/data/projects/marvisx",
      status: null,
      task_counts: {
        pending: 0,
        approved: 0,
        in_progress: 0,
        review: 0,
        completed: 0,
        rejected: 0,
        failed: 0,
      },
      last_handoff: null,
      last_status_update: null,
      on_server: true,
      path: "/data/projects/marvisx",
    },
  ],
};
