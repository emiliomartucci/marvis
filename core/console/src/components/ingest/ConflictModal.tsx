"use client";

// UX-1 (Phase 1.5): pre-upload dedup conflict resolution.
//
// Mostrato quando il preflight (`GET /api/v1/ingest/preflight`) trova un row
// non-rejected con lo stesso sha256 nel project target. Due azioni MVP:
//   - Ignora: chiudi modal, no upload
//   - Sostituisci: DELETE row esistente + upload nuovo (riutilizza fix8)
//
// "Rinomina" e' fuori scope MVP (richiede UI rename pre-upload). I file
// rejected sono auto-ri-attivati da fix4 enqueue_file → no modal necessario.

import { formatStatus } from "./format";

export interface ConflictModalProps {
  filename: string;
  existing: {
    id: string;
    status: string;
    file_path?: string;
  };
  onIgnore: () => void;
  onReplace: () => void;
  onClose: () => void;
}

export function ConflictModal({
  filename,
  existing,
  onIgnore,
  onReplace,
  onClose,
}: ConflictModalProps) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="conflict-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-sm border border-pir bg-pir-surface-0 shadow-2xl"
      >
        <header className="border-b border-pir px-5 py-3">
          <h2
            id="conflict-modal-title"
            className="font-display text-[16px] font-bold text-pir-text-primary"
          >
            File gia&apos; presente
          </h2>
        </header>
        <div className="space-y-3 px-5 py-4 font-mono text-caption text-pir-text-secondary">
          <p className="text-pir-text-primary">{filename}</p>
          <p>
            Esiste gia&apos; un record con lo stesso contenuto nel project (status:{" "}
            <span className="text-pir-accent">{formatStatus(existing.status)}</span>).
          </p>
          <p className="text-pir-text-tertiary">
            Cosa vuoi fare?
          </p>
        </div>
        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-pir px-5 py-3">
          <button
            type="button"
            onClick={onIgnore}
            className="h-8 rounded-sm border border-pir bg-transparent px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary focus:outline-none"
          >
            Ignora
          </button>
          <button
            type="button"
            onClick={onReplace}
            className="h-8 rounded-sm border border-pir-accent bg-pir-accent px-3 font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-base transition-opacity hover:opacity-90 focus:outline-none"
          >
            Sostituisci
          </button>
        </footer>
      </div>
    </div>
  );
}
