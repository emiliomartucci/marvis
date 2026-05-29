import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { IngestPendingItem } from "@/lib/types";

const { getIngestPreviewBlob, getIngestPreviewUrl } = vi.hoisted(() => ({
  getIngestPreviewBlob: vi.fn(),
  getIngestPreviewUrl: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getIngestPreviewBlob,
  getIngestPreviewUrl,
}));

import { PdfPreview } from "../PdfPreview";

const pdfItem: IngestPendingItem = {
  id: "pdf-1",
  file_path: "/data/projects/marvisx/input/invoice.pdf",
  project_slug: "marvisx",
  source_kind: "manual_upload",
  mime_type: "application/pdf",
  file_size_bytes: 1024,
  parser_used: "tier-docparse",
  extracted_text: "",
  structure: null,
  classification: null,
  status: "awaiting_triage",
  error_message: null,
  target_folder: null,
  target_filename: null,
  created_at: "2026-05-13 10:00:00",
  updated_at: "2026-05-13 10:00:00",
};

describe("PdfPreview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getIngestPreviewUrl.mockReturnValue(
      "https://api.justaskmarvis.com/api/v1/ingest/pending/pdf-1/preview.pdf"
    );
    getIngestPreviewBlob.mockResolvedValue(
      new Blob(["%PDF"], { type: "application/pdf" })
    );
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:pdf-preview"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("renders the PDF iframe from an authenticated blob URL", async () => {
    render(<PdfPreview item={pdfItem} />);

    expect(screen.getByText("Caricamento anteprima PDF")).toBeInTheDocument();

    const frame = await screen.findByTitle("PDF preview: invoice.pdf");

    await waitFor(() => {
      expect(getIngestPreviewBlob).toHaveBeenCalledWith("pdf-1", "pdf", {
        signal: expect.any(AbortSignal),
      });
    });
    expect(frame).toHaveAttribute("src", "blob:pdf-preview");
    expect(frame).not.toHaveAttribute("sandbox");
  });
});
