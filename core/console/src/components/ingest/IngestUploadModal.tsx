"use client";

import { useEffect } from "react";
import type { UploadItem, UploadSummary } from "./useUploadQueue";

interface IngestUploadModalProps {
  projectSlug: string;
  items: UploadItem[];
  summary: UploadSummary;
  running: boolean;
  onCancelItem: (id: string) => void;
  onCancelAll: () => void;
  onClose: () => void;
}

export function IngestUploadModal({
  projectSlug,
  items,
  summary,
  running,
  onCancelItem,
  onCancelAll,
  onClose,
}: IngestUploadModalProps) {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !running) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [running, onClose]);

  const percent =
    summary.total > 0
      ? Math.round(((summary.done + summary.error + summary.cancelled) / summary.total) * 100)
      : 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="flex max-h-[80vh] w-[640px] max-w-full flex-col rounded-sm border border-pir bg-pir-surface-0 shadow-xl">
        <div className="flex items-center justify-between border-b border-pir px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-pir-text-tertiary">
              {projectSlug}
            </span>
            <span className="text-pir-text-muted">/</span>
            <h3 className="text-label text-pir-text-primary">Upload</h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={running}
            className="text-pir-text-muted transition-colors hover:text-pir-text-secondary disabled:opacity-30"
            aria-label="Close"
          >
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
              <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
            </svg>
          </button>
        </div>

        <div className="border-b border-pir px-4 py-2.5">
          <div className="flex items-center justify-between gap-3">
            <span className="font-mono text-[11px] text-pir-text-secondary">
              {running ? "uploading" : "complete"} ·{" "}
              <span className="text-pir-text-primary">
                {summary.done}/{summary.total}
              </span>{" "}
              ok
              {summary.dedup > 0 && (
                <>
                  {" · "}
                  <span className="text-amber-400">{summary.dedup} dedup</span>
                </>
              )}
              {summary.skipped > 0 && (
                <>
                  {" · "}
                  <span className="text-amber-400">{summary.skipped} skipped</span>
                </>
              )}
              {summary.error > 0 && (
                <>
                  {" · "}
                  <span className="text-red-400">{summary.error} failed</span>
                </>
              )}
              {summary.cancelled > 0 && (
                <>
                  {" · "}
                  <span className="text-pir-text-muted">{summary.cancelled} cancelled</span>
                </>
              )}
            </span>
            <span className="font-mono text-[11px] tabular-nums text-pir-text-muted">
              {percent}%
            </span>
          </div>
          <div className="mt-2 h-1 overflow-hidden rounded-sm bg-pir-surface-2">
            <div
              className="h-full bg-pir-accent transition-[width] duration-200"
              style={{ width: `${percent}%` }}
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {items.map((item) => (
            <UploadRow
              key={item.id}
              item={item}
              canCancel={running && (item.status === "queued" || item.status === "uploading")}
              onCancel={() => onCancelItem(item.id)}
            />
          ))}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-pir px-4 py-3">
          {running ? (
            <button
              type="button"
              onClick={onCancelAll}
              className="rounded-sm border border-pir bg-pir-surface-1 px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-secondary transition-colors hover:border-red-500 hover:text-red-400"
            >
              Cancel all
            </button>
          ) : (
            <button
              type="button"
              onClick={onClose}
              className="rounded-sm border border-pir bg-pir-surface-2 px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-accent"
            >
              Close
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function UploadRow({
  item,
  canCancel,
  onCancel,
}: {
  item: UploadItem;
  canCancel: boolean;
  onCancel: () => void;
}) {
  const sizeKb = item.file.size / 1024;
  const sizeLabel = sizeKb >= 1024 ? `${(sizeKb / 1024).toFixed(1)} MB` : `${sizeKb.toFixed(0)} KB`;
  return (
    <div className="flex items-center gap-2 border-b border-pir/40 px-4 py-2 last:border-b-0">
      <StatusIcon status={item.status} />
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-[11px] text-pir-text-primary">
          {item.relativePath}
        </div>
        {item.error && (
          <div className="truncate text-[10px] text-red-400">{item.error}</div>
        )}
        {!item.error && item.detail && (
          <div className="truncate text-[10px] text-amber-400">{item.detail}</div>
        )}
      </div>
      <span className="shrink-0 font-mono text-[10px] tabular-nums text-pir-text-muted">
        {sizeLabel}
      </span>
      {canCancel && (
        <button
          type="button"
          onClick={onCancel}
          className="shrink-0 text-pir-text-muted transition-colors hover:text-red-400"
          aria-label="Cancel item"
        >
          <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
            <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
          </svg>
        </button>
      )}
    </div>
  );
}

function StatusIcon({ status }: { status: UploadItem["status"] }) {
  if (status === "uploading") {
    return (
      <svg
        className="h-3.5 w-3.5 shrink-0 animate-spin text-pir-accent"
        viewBox="0 0 20 20"
        fill="none"
      >
        <circle cx="10" cy="10" r="7" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
        <path
          d="M17 10a7 7 0 0 1-7 7"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
    );
  }
  if (status === "done") {
    return (
      <svg className="h-3.5 w-3.5 shrink-0 text-emerald-400" viewBox="0 0 20 20" fill="currentColor">
        <path d="M16.7 5.3a1 1 0 0 1 0 1.4l-7 7a1 1 0 0 1-1.4 0l-3.5-3.5a1 1 0 1 1 1.4-1.4L9 11.6l6.3-6.3a1 1 0 0 1 1.4 0Z" />
      </svg>
    );
  }
  if (status === "error") {
    return (
      <svg className="h-3.5 w-3.5 shrink-0 text-red-400" viewBox="0 0 20 20" fill="currentColor">
        <path d="M10 2a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm-1 4h2v6H9V6Zm0 8h2v2H9v-2Z" />
      </svg>
    );
  }
  if (status === "cancelled") {
    return (
      <svg className="h-3.5 w-3.5 shrink-0 text-pir-text-muted" viewBox="0 0 20 20" fill="currentColor">
        <path d="M10 2a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 2a6 6 0 0 1 4.74 9.66L6.34 5.26A6 6 0 0 1 10 4Zm0 12a6 6 0 0 1-4.74-9.66l8.4 8.4A6 6 0 0 1 10 16Z" />
      </svg>
    );
  }
  if (status === "dedup") {
    return (
      <svg className="h-3.5 w-3.5 shrink-0 text-amber-400" viewBox="0 0 20 20" fill="currentColor">
        <path d="M7 2a3 3 0 0 0-3 3v8a3 3 0 0 0 3 3h2v-2H7a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2h2V5a3 3 0 0 0-3-3H7Zm6 6a3 3 0 0 0-3 3v4a3 3 0 0 0 3 3h4a3 3 0 0 0 3-3v-4a3 3 0 0 0-3-3h-4Zm0 2h4a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1Z" />
      </svg>
    );
  }
  if (status === "skipped") {
    return (
      <svg className="h-3.5 w-3.5 shrink-0 text-amber-400" viewBox="0 0 20 20" fill="currentColor">
        <path d="M10 2a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm-1 4h2v5H9V6Zm0 7h2v2H9v-2Z" />
      </svg>
    );
  }
  return (
    <svg className="h-3.5 w-3.5 shrink-0 text-pir-text-muted" viewBox="0 0 20 20" fill="none">
      <circle cx="10" cy="10" r="6" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2 2" />
    </svg>
  );
}
