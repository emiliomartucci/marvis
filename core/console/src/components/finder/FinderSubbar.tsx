// v1.0.0 - 2026-04-22 - Finder subbar (h-44) v2: breadcrumb + search trigger + meta pill (PR #10)
"use client";

import type { FinderFileContent } from "@/lib/types";

interface FinderSubbarProps {
  currentPath: string;
  folderCount: number;
  fileCount: number;
  openedFile: FinderFileContent | null;
  canWrite: boolean;
  onNavigate: (path: string) => void;
  onUpload: () => void;
  onOpenSearch: () => void;
}

function fmtBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function countLines(content: string): number {
  if (!content) return 0;
  // Count newlines + 1 for last non-empty fragment.
  const nl = (content.match(/\n/g) || []).length;
  return content.endsWith("\n") ? nl : nl + 1;
}

function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  if (dot < 0 || dot === name.length - 1) return "—";
  return name.slice(dot + 1).toLowerCase();
}

export default function FinderSubbar({
  currentPath,
  folderCount,
  fileCount,
  openedFile,
  canWrite,
  onNavigate,
  onUpload,
  onOpenSearch,
}: FinderSubbarProps) {
  const parts = currentPath ? currentPath.split("/").filter(Boolean) : [];
  const ext = openedFile ? extOf(openedFile.filename) : "";
  const lines = openedFile && openedFile.encoding === "utf-8" ? countLines(openedFile.content) : null;

  return (
    <div
      className="bg-pir-surface-0 border-b border-pir flex items-center shrink-0"
      style={{ height: 44 }}
    >
      {/* Left: eyebrow + counts + upload — aligned with tree width 280 */}
      <div
        className="flex items-center justify-between border-r border-pir h-full"
        style={{ width: 280, padding: "0 16px" }}
      >
        <span className="flex items-center gap-1.5 text-pir-text-primary">
          <span
            className="text-pir-text-tertiary uppercase"
            style={{
              fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
              fontWeight: 600,
              fontSize: 10,
              letterSpacing: "0.22em",
              lineHeight: 1,
            }}
          >
            Finder
          </span>
          <span
            className="text-pir-text-muted tabular-nums"
            style={{
              fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
              fontSize: 10,
              letterSpacing: "0.08em",
            }}
          >
            · {folderCount}f / {fileCount}
          </span>
        </span>
        {canWrite && (
          <button
            type="button"
            onClick={onUpload}
            title="Upload files"
            aria-label="Upload files"
            className="p-1 text-pir-text-tertiary hover:text-pir-accent transition-colors"
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          </button>
        )}
      </div>

      {/* Right: breadcrumb + search + meta */}
      <div className="flex-1 flex items-center gap-3.5 min-w-0" style={{ padding: "0 14px" }}>
        <nav
          className="flex items-center gap-1 min-w-0 text-pir-text-primary"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 12,
            fontWeight: 500,
          }}
          aria-label="Breadcrumb"
        >
          <button
            type="button"
            onClick={() => onNavigate("")}
            className={`${
              parts.length === 0 ? "text-pir-text-primary" : "text-pir-text-tertiary hover:text-pir-accent"
            } transition-colors`}
            style={{ fontWeight: parts.length === 0 ? 600 : 500 }}
          >
            ~
          </button>
          {parts.map((part, i) => {
            const partPath = parts.slice(0, i + 1).join("/");
            const isLast = i === parts.length - 1;
            return (
              <span key={partPath} className="flex items-center gap-1 min-w-0">
                <span className="text-pir-text-muted" aria-hidden>
                  /
                </span>
                <button
                  type="button"
                  onClick={() => onNavigate(partPath)}
                  className={`truncate transition-colors ${
                    isLast
                      ? "text-pir-text-primary"
                      : "text-pir-text-tertiary hover:text-pir-accent"
                  }`}
                  style={{ maxWidth: 160, fontWeight: isLast ? 600 : 500 }}
                >
                  {part}
                </button>
              </span>
            );
          })}
        </nav>

        {/* Meta pill — shown when file is open */}
        {openedFile && (
          <span
            className="inline-flex items-center gap-1.5 border border-pir rounded-[2px] px-2 py-[3px] text-pir-text-secondary shrink-0"
            style={{
              fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
              fontSize: 10,
              fontWeight: 500,
            }}
            title={openedFile.path}
          >
            <span>{fmtBytes(openedFile.size)}</span>
            {lines != null && (
              <>
                <span className="text-pir-text-muted" aria-hidden>
                  ·
                </span>
                <span>
                  {lines} line{lines === 1 ? "" : "s"}
                </span>
              </>
            )}
            <span className="text-pir-text-muted" aria-hidden>
              ·
            </span>
            <span className="text-pir-accent uppercase">{ext}</span>
          </span>
        )}

        {/* Search trigger — opens ⌘K palette */}
        <button
          type="button"
          onClick={onOpenSearch}
          className="ml-auto inline-flex items-center gap-1.5 border border-pir hover:border-pir-accent/50 text-pir-text-tertiary hover:text-pir-accent transition-colors shrink-0"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            padding: "4px 8px",
            borderRadius: 2,
          }}
          title="Search files (⌘K)"
          aria-label="Open global search"
        >
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <span>Search</span>
          <kbd
            className="text-pir-text-muted"
            style={{ fontFamily: "inherit", fontSize: 9 }}
          >
            ⌘K
          </kbd>
        </button>
      </div>
    </div>
  );
}
