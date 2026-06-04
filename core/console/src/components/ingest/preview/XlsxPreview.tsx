"use client";

import { getIngestPreviewUrl } from "@/lib/api";
import type { IngestPendingItem } from "@/lib/types";
import { fileLabel, formatBytes } from "../format";

type SheetPreview = {
  name?: string;
  headers?: unknown[];
  rows?: unknown[][];
};

export function XlsxPreview({ item }: { item: IngestPendingItem }) {
  const sheets = extractSheets(item.structure);
  const firstSheet = sheets[0];
  const previewUrl = getIngestPreviewUrl(item.id, "xlsx");

  if (!firstSheet) {
    return (
      <div className="flex min-h-[520px] items-center justify-center px-6 text-center">
        <div className="max-w-md">
          <p className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
            XLSX preview
          </p>
          <h3 className="mt-2 font-display text-heading text-pir-text-primary">
            Tabella non ancora estratta.
          </h3>
          <p className="mt-2 font-sans text-body text-pir-text-secondary">
            {fileLabel(item)} e&apos; disponibile come file originale. La preview
            strutturata arriva con lo story E4.2.
          </p>
          <a
            href={previewUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-5 inline-flex h-8 items-center rounded-sm border border-pir-accent bg-pir-accent/10 px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-accent transition-colors hover:bg-pir-accent/15 focus:border-pir-accent focus:outline-none"
          >
            Scarica XLSX
          </a>
        </div>
      </div>
    );
  }

  const headers = firstSheet.headers?.slice(0, 10) ?? [];
  const rows = firstSheet.rows?.slice(0, 5) ?? [];

  return (
    <div className="flex min-h-full flex-col bg-pir-base">
      <header className="flex shrink-0 items-center justify-between border-b border-pir bg-pir-surface-0 px-4 py-3">
        <div>
          <h3 className="font-display text-heading text-pir-text-primary">
            {firstSheet.name || "Sheet 1"}
          </h3>
          <p className="mt-1 font-mono text-caption text-pir-text-tertiary">
            {formatBytes(item.file_size_bytes)} · prime 5 righe
          </p>
        </div>
        <a
          href={previewUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded-sm border border-pir bg-pir-surface-1 px-2 py-1 font-mono text-caption uppercase text-pir-text-tertiary hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
        >
          Open
        </a>
      </header>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <table className="w-full min-w-[720px] border-collapse font-mono text-caption tabular-nums">
          <thead>
            <tr>
              {headers.map((header, index) => (
                <th
                  key={index}
                  className="border border-pir bg-pir-surface-1 px-3 py-2 text-left font-semibold uppercase tracking-[0.08em] text-pir-text-tertiary"
                >
                  {String(header)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {headers.map((_, cellIndex) => (
                  <td
                    key={cellIndex}
                    className="border border-pir bg-pir-surface-0 px-3 py-2 text-pir-text-secondary"
                  >
                    {String(row[cellIndex] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function extractSheets(structure: Record<string, unknown> | null): SheetPreview[] {
  const sheets = structure?.sheets;
  if (!Array.isArray(sheets)) return [];
  return sheets.filter((sheet): sheet is SheetPreview => {
    if (sheet == null || typeof sheet !== "object") return false;
    const candidate = sheet as SheetPreview;
    return Array.isArray(candidate.headers) || Array.isArray(candidate.rows);
  });
}
