"use client";

import { useState } from "react";

interface FinderToolbarProps {
  currentPath: string;
  onNavigate: (path: string) => void;
  onRefresh: () => void;
}

export default function FinderToolbar({
  currentPath,
  onNavigate,
  onRefresh,
}: FinderToolbarProps) {
  const [copied, setCopied] = useState(false);
  const parts = currentPath ? currentPath.split("/").filter(Boolean) : [];

  function copyPath() {
    navigator.clipboard.writeText(currentPath || "~").then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  }

  return (
    <div className="flex items-center gap-2 border-b border-pir bg-pir-surface-0 px-4 py-2">
      <nav className="flex min-w-0 flex-1 items-center gap-0.5 text-caption" aria-label="Finder path">
        <button
          type="button"
          onClick={() => onNavigate("")}
          className="shrink-0 text-pir-text-muted transition-colors hover:text-pir-accent"
        >
          ~
        </button>
        {parts.map((part, index) => {
          const partPath = parts.slice(0, index + 1).join("/");
          const isLast = index === parts.length - 1;
          return (
            <span key={partPath} className="flex min-w-0 items-center gap-0.5">
              <span className="text-pir-text-muted">/</span>
              <button
                type="button"
                onClick={() => onNavigate(partPath)}
                className={`max-w-[120px] truncate transition-colors ${isLast ? "text-pir-text-primary" : "text-pir-text-muted hover:text-pir-accent"}`}
              >
                {part}
              </button>
            </span>
          );
        })}
      </nav>
      <button
        type="button"
        onClick={copyPath}
        className="rounded px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-secondary"
      >
        {copied ? "Copied" : "Copy path"}
      </button>
      <button
        type="button"
        onClick={onRefresh}
        className="rounded px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-secondary"
      >
        Refresh
      </button>
    </div>
  );
}
