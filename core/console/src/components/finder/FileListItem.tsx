"use client";

import { useState } from "react";
import type { FinderListItem } from "@/lib/types";

interface FileListItemProps {
  item: FinderListItem;
  selected: boolean;
  multiSelected: boolean;
  highlighted?: boolean;
  compact?: boolean;
  onClick: (e: React.MouseEvent) => void;
  onDoubleClick: () => void;
  onDownload: () => void;
  onRename: () => void;
  onDelete: () => void;
  onMove: () => void;
  onContextMenu?: (e: React.MouseEvent, item: FinderListItem) => void;
}

function formatSize(bytes: number): string {
  if (bytes === 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (diffDays === 0) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (diffDays < 7) return `${diffDays}d ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function getIcon(item: FinderListItem) {
  if (item.is_dir) {
    return (
      <svg className="w-4 h-4 text-pir-accent/70" viewBox="0 0 20 20" fill="currentColor">
        <path d="M3.75 3A1.75 1.75 0 002 4.75v3.26a3.235 3.235 0 011.75-.51h12.5c.644 0 1.245.188 1.75.51V6.75A1.75 1.75 0 0016.25 5h-4.836a.25.25 0 01-.177-.073L9.823 3.513A1.75 1.75 0 008.586 3H3.75zM3.75 9A1.75 1.75 0 002 10.75v4.5c0 .966.784 1.75 1.75 1.75h12.5A1.75 1.75 0 0018 15.25v-4.5A1.75 1.75 0 0016.25 9H3.75z" />
      </svg>
    );
  }
  const ext = item.extension?.toLowerCase();
  const codeExts = [".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".sql", ".sh", ".toml", ".cfg"];
  const docExts = [".md", ".txt", ".rst", ".csv"];

  if (ext && codeExts.includes(ext)) {
    return (
      <svg className="w-4 h-4 text-green-400/70" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M6.28 5.22a.75.75 0 010 1.06L2.56 10l3.72 3.72a.75.75 0 01-1.06 1.06L.97 10.53a.75.75 0 010-1.06l4.25-4.25a.75.75 0 011.06 0zm7.44 0a.75.75 0 011.06 0l4.25 4.25a.75.75 0 010 1.06l-4.25 4.25a.75.75 0 01-1.06-1.06L17.44 10l-3.72-3.72a.75.75 0 010-1.06z" clipRule="evenodd" />
      </svg>
    );
  }
  if (ext && docExts.includes(ext)) {
    return (
      <svg className="w-4 h-4 text-blue-400/70" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M4.5 2A1.5 1.5 0 003 3.5v13A1.5 1.5 0 004.5 18h11a1.5 1.5 0 001.5-1.5V7.621a1.5 1.5 0 00-.44-1.06l-4.12-4.122A1.5 1.5 0 0011.378 2H4.5zm2.25 8.5a.75.75 0 000 1.5h6.5a.75.75 0 000-1.5h-6.5zm0 3a.75.75 0 000 1.5h6.5a.75.75 0 000-1.5h-6.5z" clipRule="evenodd" />
      </svg>
    );
  }
  return (
    <svg className="w-4 h-4 text-pir-text-muted" viewBox="0 0 20 20" fill="currentColor">
      <path d="M3 3.5A1.5 1.5 0 014.5 2h6.879a1.5 1.5 0 011.06.44l4.122 4.12A1.5 1.5 0 0117 7.622V16.5a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 013 16.5v-13z" />
    </svg>
  );
}

export default function FileListItem({
  item,
  selected,
  multiSelected,
  highlighted = false,
  compact = false,
  onClick,
  onDoubleClick,
  onDownload,
  onRename,
  onDelete,
  onMove,
  onContextMenu,
}: FileListItemProps) {
  const isHighlighted = selected || multiSelected;
  const [copied, setCopied] = useState(false);

  return (
    <button
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      onContextMenu={(e) => {
        if (onContextMenu) onContextMenu(e, item);
      }}
      className={`group w-full flex items-center gap-2 px-3 py-1.5 text-left transition-colors ${
        highlighted
          ? "bg-pir-warning/15 text-pir-text-primary ring-1 ring-inset ring-pir-warning/40"
          : isHighlighted
          ? "bg-pir-accent/10 text-pir-text-primary"
          : "text-pir-text-secondary hover:bg-pir-surface-1"
      }`}
    >
      {/* Checkbox for multi-select */}
      <div
        className={`w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0 transition-colors ${
          multiSelected
            ? "bg-pir-accent border-pir-accent"
            : "border-pir-border group-hover:border-pir-text-muted"
        }`}
        onClick={(e) => {
          e.stopPropagation();
          // Toggle via ctrl-click simulation
          onClick({ ...e, ctrlKey: true, metaKey: true } as unknown as React.MouseEvent);
        }}
      >
        {multiSelected && (
          <svg className="w-2.5 h-2.5 text-white" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clipRule="evenodd" />
          </svg>
        )}
      </div>

      {getIcon(item)}
      <span className="flex-1 min-w-0 truncate text-caption">
        {item.name}
      </span>

      {/* Hover action buttons */}
      <div className="hidden group-hover:flex items-center gap-0.5 shrink-0">
        <button
          onClick={(e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(item.path);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
          className="p-0.5 text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
          title="Copy path"
        >
          {copied ? (
            <svg className="w-3 h-3 text-pir-accent" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" clipRule="evenodd" />
            </svg>
          ) : (
            <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
              <path d="M7 3.5A1.5 1.5 0 018.5 2h3.879a1.5 1.5 0 011.06.44l3.122 3.12A1.5 1.5 0 0117 6.622V12.5a1.5 1.5 0 01-1.5 1.5h-1v-3.379a3 3 0 00-.879-2.121L10.5 5.379A3 3 0 008.379 4.5H7v-1z" />
              <path d="M4.5 6A1.5 1.5 0 003 7.5v9A1.5 1.5 0 004.5 18h7a1.5 1.5 0 001.5-1.5v-5.879a1.5 1.5 0 00-.44-1.06L9.44 6.44A1.5 1.5 0 008.378 6H4.5z" />
            </svg>
          )}
        </button>
        {!item.is_dir && (
          <button
            onClick={(e) => { e.stopPropagation(); onDownload(); }}
            className="p-0.5 text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
            title="Download"
          >
            <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
              <path d="M10.75 2.75a.75.75 0 00-1.5 0v8.614L6.295 8.235a.75.75 0 10-1.09 1.03l4.25 4.5a.75.75 0 001.09 0l4.25-4.5a.75.75 0 00-1.09-1.03l-2.955 3.129V2.75z" />
              <path d="M3.5 12.75a.75.75 0 00-1.5 0v2.5A2.75 2.75 0 004.75 18h10.5A2.75 2.75 0 0018 15.25v-2.5a.75.75 0 00-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5z" />
            </svg>
          </button>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onRename(); }}
          className="p-0.5 text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
          title="Rename"
        >
          <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
            <path d="M2.695 14.763l-1.262 3.154a.5.5 0 00.65.65l3.155-1.262a4 4 0 001.343-.885L17.5 5.5a2.121 2.121 0 00-3-3L3.58 13.42a4 4 0 00-.885 1.343z" />
          </svg>
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onMove(); }}
          className="p-0.5 text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
          title="Move"
        >
          <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M2 10a.75.75 0 01.75-.75h12.59l-2.1-1.95a.75.75 0 111.02-1.1l3.5 3.25a.75.75 0 010 1.1l-3.5 3.25a.75.75 0 11-1.02-1.1l2.1-1.95H2.75A.75.75 0 012 10z" clipRule="evenodd" />
          </svg>
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onDelete(); }}
          className="p-0.5 text-pir-text-muted hover:text-red-400 rounded transition-colors"
          title="Delete"
        >
          <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M8.75 1A2.75 2.75 0 006 3.75v.443c-.795.077-1.584.176-2.365.298a.75.75 0 10.23 1.482l.149-.022.841 10.518A2.75 2.75 0 007.596 19h4.807a2.75 2.75 0 002.742-2.53l.841-10.52.149.023a.75.75 0 00.23-1.482A41.03 41.03 0 0014 4.193V3.75A2.75 2.75 0 0011.25 1h-2.5zM10 4c.84 0 1.673.025 2.5.075V3.75c0-.69-.56-1.25-1.25-1.25h-2.5c-.69 0-1.25.56-1.25 1.25v.325C8.327 4.025 9.16 4 10 4zM8.58 7.72a.75.75 0 00-1.5.06l.3 7.5a.75.75 0 101.5-.06l-.3-7.5zm4.34.06a.75.75 0 10-1.5-.06l-.3 7.5a.75.75 0 101.5.06l.3-7.5z" clipRule="evenodd" />
          </svg>
        </button>
      </div>

      {/* Size/date - only when no hover and not compact */}
      {!compact && (
        <div className="group-hover:hidden flex items-center gap-0">
          <span className="text-caption text-pir-text-muted shrink-0 w-16 text-right">
            {formatSize(item.size)}
          </span>
          <span className="text-caption text-pir-text-muted shrink-0 w-16 text-right">
            {formatDate(item.modified)}
          </span>
        </div>
      )}
    </button>
  );
}
