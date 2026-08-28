"use client";

import { useState } from "react";
import type { FinderListItem } from "@/lib/types";

interface FileListItemProps {
  item: FinderListItem;
  selected: boolean;
  highlighted?: boolean;
  compact?: boolean;
  onClick: () => void;
  onDoubleClick: () => void;
  onDownload: () => void;
}

function formatSize(bytes: number): string {
  if (bytes === 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString();
}

export default function FileListItem({
  item,
  selected,
  highlighted = false,
  compact = false,
  onClick,
  onDoubleClick,
  onDownload,
}: FileListItemProps) {
  const [copied, setCopied] = useState(false);

  function copyPath(event: React.MouseEvent) {
    event.stopPropagation();
    navigator.clipboard.writeText(item.path).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onClick();
      }}
      className={`group flex w-full items-center gap-2 px-3 py-2 text-left transition-colors ${selected ? "bg-pir-accent/10" : highlighted ? "bg-pir-success/10" : "hover:bg-pir-surface-1"}`}
    >
      <span className={item.is_dir ? "text-pir-accent" : "text-pir-text-tertiary"} aria-hidden>
        {item.is_dir ? "▸" : "•"}
      </span>
      <span className="min-w-0 flex-1 truncate text-caption text-pir-text-primary">{item.name}</span>
      {!compact && (
        <>
          <span className="shrink-0 text-caption text-pir-text-muted">{formatSize(item.size)}</span>
          <span className="shrink-0 text-caption text-pir-text-muted">{formatDate(item.modified)}</span>
        </>
      )}
      <span className="flex shrink-0 items-center gap-1">
        <button
          type="button"
          onClick={copyPath}
          className="rounded p-1 text-pir-text-muted hover:text-pir-text-secondary"
          aria-label="Copy path"
        >
          {copied ? "✓" : "⧉"}
        </button>
        {!item.is_dir && (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onDownload();
            }}
            className="rounded p-1 text-pir-text-muted hover:text-pir-text-secondary"
            aria-label="Download"
          >
            ↓
          </button>
        )}
      </span>
    </div>
  );
}
