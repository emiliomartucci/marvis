"use client";

import type { FinderFileContent } from "@/lib/types";

interface FinderSubbarProps {
  currentPath: string;
  folderCount: number;
  fileCount: number;
  openedFile: FinderFileContent | null;
  onNavigate: (path: string) => void;
  onOpenSearch: () => void;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function FinderSubbar({
  currentPath,
  folderCount,
  fileCount,
  openedFile,
  onNavigate,
  onOpenSearch,
}: FinderSubbarProps) {
  const parts = currentPath ? currentPath.split("/").filter(Boolean) : [];

  return (
    <header className="flex h-11 shrink-0 items-center border-b border-pir bg-pir-surface-0">
      <div className="flex h-full w-[280px] items-center border-r border-pir px-4">
        <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.22em] text-pir-text-primary">
          Finder
        </span>
        <span className="ml-2 font-mono text-[10px] text-pir-text-muted">
          {folderCount} folders / {fileCount} files
        </span>
      </div>
      <div className="flex min-w-0 flex-1 items-center gap-3 px-4">
        <nav className="min-w-0 flex-1 truncate font-mono text-caption" aria-label="Breadcrumb">
          <button type="button" onClick={() => onNavigate("")} className="text-pir-text-tertiary hover:text-pir-accent">~</button>
          {parts.map((part, index) => {
            const path = parts.slice(0, index + 1).join("/");
            return (
              <span key={path}>
                <span className="text-pir-text-muted"> / </span>
                <button type="button" onClick={() => onNavigate(path)} className="text-pir-text-secondary hover:text-pir-accent">
                  {part}
                </button>
              </span>
            );
          })}
        </nav>
        {openedFile && (
          <span className="max-w-[240px] truncate font-mono text-[10px] text-pir-text-tertiary">
            {openedFile.filename} · {openedFile.mime_type ?? "unknown"} · {formatBytes(openedFile.size)}
          </span>
        )}
        <button type="button" onClick={onOpenSearch} className="rounded px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-secondary">
          Search
        </button>
      </div>
    </header>
  );
}
