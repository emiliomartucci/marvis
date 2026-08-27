import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { IngestPendingItem } from "@/lib/types";
import { PendingDetail } from "../PendingDetail";

const item: IngestPendingItem = {
  id: "ingest-1",
  file_path: "/data/projects/marvisx/input/README-1.md",
  project_slug: "marvisx",
  source_kind: "manual_upload",
  mime_type: "text/markdown",
  file_size_bytes: 128,
  parser_used: "tier-fast",
  extracted_text: "fallback extract that must not satisfy the heading assertion",
  structure: null,
  classification: null,
  status: "awaiting_triage",
  error_message: null,
  target_folder: "docs",
  target_filename: "README-1.md",
  created_at: "2026-05-08 09:00:00",
  updated_at: "2026-05-08 09:00:00",
};

describe("PendingDetail", () => {
  it("keeps the inspection panes and removes every mutation control", () => {
    const previewText = `# README
`;
    render(<PendingDetail item={item} previewText={previewText} />);

    expect(screen.getByText("README-1.md")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "README" })).toBeInTheDocument();

    for (const name of ["Anteprima", "Estratto", "Proposta KG", "saga", "raw"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }

    expect(screen.queryByRole("button", { name: /approve|reject|delete|retry|save|edit/i })).not.toBeInTheDocument();
  });
});
