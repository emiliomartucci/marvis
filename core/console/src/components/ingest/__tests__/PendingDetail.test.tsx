import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { IngestPendingItem } from "@/lib/types";

const { deleteIngestPending, patchIngestPending } = vi.hoisted(() => ({
  deleteIngestPending: vi.fn(),
  patchIngestPending: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  APIError: class APIError extends Error {
    status: number;
    detail: unknown;

    constructor(message: string, status: number, detail?: unknown) {
      super(message);
      this.name = "APIError";
      this.status = status;
      this.detail = detail;
    }
  },
  deleteIngestPending,
  patchIngestPending,
}));

vi.mock("@/components/ProjectSelectorModal", () => ({
  default: () => <div data-testid="project-selector-modal" />,
}));

import { PendingDetail } from "../PendingDetail";

const item: IngestPendingItem = {
  id: "ingest-1",
  file_path: "/data/projects/marvisx/input/README-1.md",
  project_slug: "marvisx",
  source_kind: "manual_upload",
  mime_type: "text/markdown",
  file_size_bytes: 128,
  parser_used: "tier-fast",
  extracted_text: "# README\n",
  structure: null,
  classification: {
    type: "doc",
    title: "README-1",
    confidence: 0.9,
    reason: "test",
  },
  status: "awaiting_triage",
  error_message: null,
  target_folder: "docs",
  target_filename: "README-1.md",
  created_at: "2026-05-08 09:00:00",
  updated_at: "2026-05-08 09:00:00",
};

function renderPendingDetail(onRefresh = vi.fn()) {
  render(
    <PendingDetail
      item={item}
      previewText="# README\n"
      onApprove={vi.fn()}
      onReject={vi.fn()}
      onRetryParse={vi.fn()}
      onRefresh={onRefresh}
    />
  );
}

function renderPendingDetailWithItem(customItem: IngestPendingItem) {
  render(
    <PendingDetail
      item={customItem}
      previewText="# README\n"
      onApprove={vi.fn()}
      onReject={vi.fn()}
      onRetryParse={vi.fn()}
      onRefresh={vi.fn()}
    />
  );
}

describe("PendingDetail delete confirmation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    deleteIngestPending.mockResolvedValue(undefined);
    patchIngestPending.mockResolvedValue(item);
  });

  it("uses a non-blocking dialog instead of window.confirm", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    renderPendingDetail();

    await user.click(screen.getByRole("button", { name: "Elimina" }));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(deleteIngestPending).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Eliminare file" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Annulla" }));

    expect(screen.queryByRole("dialog", { name: "Eliminare file" })).not.toBeInTheDocument();
    expect(deleteIngestPending).not.toHaveBeenCalled();
  });

  it("deletes the ingest row only after confirming the dialog", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    renderPendingDetail(onRefresh);

    await user.click(screen.getByRole("button", { name: "Elimina" }));
    const dialog = screen.getByRole("dialog", { name: "Eliminare file" });

    await user.click(within(dialog).getByRole("button", { name: "Elimina" }));

    await waitFor(() => {
      expect(deleteIngestPending).toHaveBeenCalledWith("ingest-1");
      expect(onRefresh).toHaveBeenCalled();
    });
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("shows pipeline route and source prior diagnostics", async () => {
    const user = userEvent.setup();
    renderPendingDetailWithItem({
      ...item,
      structure: {
        ingest_v2: {
          route: {
            workflow: "vision",
            tier: "tier-vision",
            confidence: 0.78,
            reason: "image needs visual context",
          },
          parser_quality: { score: 0.68 },
          source_context: {
            project_slug: "site-frankfurt-buildout",
            prior: 0.95,
            reason: "terminal_upload_project_input",
          },
          image_probe: {
            image_kind: "screenshot",
            screenshot_likelihood: 0.72,
            document_likelihood: 0.22,
            photo_likelihood: 0.12,
            text_likelihood: 0.42,
            signals: ["screenshot_hint"],
          },
        },
      },
      classification: {
        ...item.classification,
        llm_metadata: {
          model: "tier-fast",
          provider: "local",
          project_slug: "site-frankfurt-buildout",
          source_project_slug: "site-frankfurt-buildout",
          source_project_prior: 0.95,
          source_project_reason: "terminal_upload_project_input",
          source_project_followed: true,
        },
      },
    });

    await user.click(screen.getByRole("button", { name: "Proposta KG" }));

    expect(screen.getByText("Pipeline diagnostics")).toBeInTheDocument();
    expect(screen.getByText("tier-vision")).toBeInTheDocument();
    expect(screen.getAllByText("site-frankfurt-buildout").length).toBeGreaterThan(0);
    expect(screen.getByText("site-frankfurt-buildout 95% · followed")).toBeInTheDocument();
  });
});
