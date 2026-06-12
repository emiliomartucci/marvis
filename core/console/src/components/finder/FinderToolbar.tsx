// v1.3.0 - 2026-03-29 - Add semantic search dropdown with debounced API call
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useSemanticSearch } from "@/hooks/useSemanticSearch";
import type { SearchHit } from "@/lib/api";
import UploadModal from "./UploadModal";

const DOC_TYPE_LABEL: Record<string, string> = {
  task: "Task",
  project: "Project",
  file: "File",
  handoff: "Handoff",
};

function urlFor(hit: SearchHit): string {
  switch (hit.doc_type) {
    case "task":
      return `/triage/?task=${encodeURIComponent(hit.doc_id)}`;
    case "project":
      return `/projects/detail/?slug=${encodeURIComponent(hit.project)}`;
    case "file": {
      const parts = (hit.path ?? "").split("/");
      const dir = parts.slice(0, -1).join("/");
      const file = parts[parts.length - 1] ?? "";
      return `/finder/?path=${encodeURIComponent(dir)}&highlight=${encodeURIComponent(file)}`;
    }
    case "handoff":
      return `/projects/detail/?slug=${encodeURIComponent(hit.project)}&highlight=${encodeURIComponent(hit.doc_id)}`;
  }
}

interface FinderToolbarProps {
  currentPath: string;
  searchQuery: string;
  selectedCount: number;
  canWrite: boolean;
  canAdmin: boolean;
  onNavigate: (path: string) => void;
  onRefresh: () => void;
  onSearchChange: (query: string) => void;
  onNewFolder: () => void;
  onBulkDelete: () => void;
  onBulkDownload: () => void;
  onClearSelection: () => void;
}

export default function FinderToolbar({
  currentPath,
  searchQuery,
  selectedCount,
  canWrite,
  canAdmin,
  onNavigate,
  onRefresh,
  onSearchChange,
  onNewFolder,
  onBulkDelete,
  onBulkDownload,
  onClearSelection,
}: FinderToolbarProps) {
  const [showUpload, setShowUpload] = useState(false);
  const [copied, setCopied] = useState(false);
  const [searchFocused, setSearchFocused] = useState(false);
  const router = useRouter();
  const semantic = useSemanticSearch(300);

  const parts = currentPath ? currentPath.split("/").filter(Boolean) : [];

  const handleCopyPath = () => {
    navigator.clipboard.writeText(currentPath || "~");
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="flex flex-col bg-pir-surface-0 border-b border-pir shrink-0">
      {/* Top row: breadcrumb + actions */}
      <div className="flex items-center gap-2 px-4 py-2">
        {/* Breadcrumb */}
        <nav className="flex items-center gap-0.5 text-caption min-w-0 flex-1">
          <button
            onClick={() => onNavigate("")}
            className="text-pir-text-muted hover:text-pir-accent transition-colors shrink-0"
          >
            ~
          </button>
          {parts.map((part, i) => {
            const partPath = parts.slice(0, i + 1).join("/");
            const isLast = i === parts.length - 1;
            return (
              <span key={partPath} className="flex items-center gap-0.5 min-w-0">
                <span className="text-pir-text-muted">/</span>
                <button
                  onClick={() => onNavigate(partPath)}
                  className={`truncate max-w-[120px] transition-colors ${
                    isLast
                      ? "text-pir-text-primary"
                      : "text-pir-text-muted hover:text-pir-accent"
                  }`}
                >
                  {part}
                </button>
              </span>
            );
          })}
        </nav>

        {/* Copy path */}
        <button
          onClick={handleCopyPath}
          className="p-1 text-pir-text-muted hover:text-pir-text-secondary transition-colors shrink-0"
          title="Copy current path"
        >
          {copied ? (
            <svg className="w-3.5 h-3.5 text-pir-accent" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
                clipRule="evenodd"
              />
            </svg>
          ) : (
            <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
              <path d="M7 3.5A1.5 1.5 0 018.5 2h3.879a1.5 1.5 0 011.06.44l3.122 3.12A1.5 1.5 0 0117 6.622V12.5a1.5 1.5 0 01-1.5 1.5h-1v-3.379a3 3 0 00-.879-2.121L10.5 5.379A3 3 0 008.379 4.5H7v-1z" />
              <path d="M4.5 6A1.5 1.5 0 003 7.5v9A1.5 1.5 0 004.5 18h7a1.5 1.5 0 001.5-1.5v-5.879a1.5 1.5 0 00-.44-1.06L9.44 6.44A1.5 1.5 0 008.378 6H4.5z" />
            </svg>
          )}
        </button>

        {/* Search — dual mode: instant local filter + debounced semantic */}
        <div className="relative w-56">
          {semantic.loading ? (
            <svg className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-pir-accent animate-spin pointer-events-none" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
          ) : (
            <svg
              className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-pir-text-muted pointer-events-none"
              viewBox="0 0 20 20"
              fill="currentColor"
            >
              <path
                fillRule="evenodd"
                d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
                clipRule="evenodd"
              />
            </svg>
          )}
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => {
              onSearchChange(e.target.value);
              semantic.search(e.target.value);
            }}
            onFocus={() => setSearchFocused(true)}
            onBlur={() => setTimeout(() => setSearchFocused(false), 200)}
            placeholder="Search..."
            className="w-full pl-7 pr-2 py-1 text-caption bg-pir-surface-1 border border-pir rounded text-pir-text-secondary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent"
          />

          {/* Semantic search dropdown — only files and handoffs (docs .md), no tasks/projects */}
          {searchFocused && semantic.query.trim().length > 0 && (() => {
            const docHits = semantic.allHits.filter((h) => h.doc_type === "file" || h.doc_type === "handoff");
            return (
            <div className="absolute top-full left-0 right-0 mt-1 bg-pir-surface-0 border border-pir rounded-lg shadow-xl z-50 max-h-64 overflow-y-auto">
              {semantic.error && (
                <div className="px-3 py-2 text-caption text-pir-error text-center">{semantic.error}</div>
              )}
              {!semantic.loading && !semantic.error && docHits.length === 0 && semantic.results && (
                <div className="px-3 py-2 text-caption text-pir-text-muted text-center">
                  No results
                </div>
              )}
              {docHits.map((hit, i) => (
                <button
                  key={`${hit.doc_type}-${hit.doc_id}-${i}`}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    router.push(urlFor(hit));
                    semantic.clear();
                    onSearchChange("");
                  }}
                  className="w-full px-3 py-1.5 text-left hover:bg-pir-surface-1 transition-colors flex items-start gap-2 group"
                >
                  <span className="text-[10px] text-pir-text-muted mt-0.5 shrink-0 w-12">
                    {DOC_TYPE_LABEL[hit.doc_type] || hit.doc_type}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="text-caption text-pir-text-primary truncate group-hover:text-pir-accent">
                      {hit.title}
                    </div>
                    <div className="text-[10px] text-pir-text-muted truncate">{hit.project}</div>
                  </div>
                  <span className="text-[10px] text-pir-text-muted shrink-0 opacity-0 group-hover:opacity-100">
                    {Math.round(hit.score * 100)}%
                  </span>
                </button>
              ))}
            </div>
            );
          })()}
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1 shrink-0">
          {canWrite && (
            <button
              onClick={onNewFolder}
              className="p-1.5 text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
              title="New folder"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
                <path d="M3.75 3A1.75 1.75 0 002 4.75v3.26a3.235 3.235 0 011.75-.51h12.5c.644 0 1.245.188 1.75.51V6.75A1.75 1.75 0 0016.25 5h-4.836a.25.25 0 01-.177-.073L9.823 3.513A1.75 1.75 0 008.586 3H3.75z" />
                <path d="M3.75 9A1.75 1.75 0 002 10.75v4.5c0 .966.784 1.75 1.75 1.75h12.5A1.75 1.75 0 0018 15.25v-4.5A1.75 1.75 0 0016.25 9H3.75z" />
              </svg>
            </button>
          )}
          <button
            onClick={onRefresh}
            className="p-1.5 text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
            title="Refresh"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H4.28a.75.75 0 00-.75.75v3.955a.75.75 0 001.5 0v-2.134l.235.234A7 7 0 0017.25 10a.75.75 0 00-1.5 0 5.5 5.5 0 01-.438 2.424zM4.688 8.576a5.5 5.5 0 019.201-2.466l.312.311h-2.433a.75.75 0 000 1.5h3.952a.75.75 0 00.75-.75V3.216a.75.75 0 00-1.5 0v2.134l-.235-.234A7 7 0 002.75 10a.75.75 0 001.5 0 5.5 5.5 0 01.438-2.424z"
                clipRule="evenodd"
              />
            </svg>
          </button>
          {canWrite && (
            <button
              onClick={() => setShowUpload(true)}
              className="p-1.5 text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
              title="Upload"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
                <path d="M9.25 13.25a.75.75 0 001.5 0V4.636l2.955 3.129a.75.75 0 001.09-1.03l-4.25-4.5a.75.75 0 00-1.09 0l-4.25 4.5a.75.75 0 101.09 1.03L9.25 4.636v8.614z" />
                <path d="M3.5 12.75a.75.75 0 00-1.5 0v2.5A2.75 2.75 0 004.75 18h10.5A2.75 2.75 0 0018 15.25v-2.5a.75.75 0 00-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5z" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Bulk actions bar */}
      {selectedCount > 0 && (
        <div className="flex items-center gap-2 px-4 py-1.5 bg-pir-accent/5 border-t border-pir">
          <span className="text-caption text-pir-accent">
            {selectedCount} selected
          </span>
          <button
            onClick={onBulkDownload}
            className="px-2 py-0.5 text-caption text-pir-text-secondary hover:text-pir-text-primary rounded transition-colors"
          >
            Download
          </button>
          {canAdmin && (
            <button
              onClick={onBulkDelete}
              className="px-2 py-0.5 text-caption text-red-400 hover:text-red-300 rounded transition-colors"
            >
              Delete
            </button>
          )}
          <button
            onClick={onClearSelection}
            className="ml-auto px-2 py-0.5 text-caption text-pir-text-muted hover:text-pir-text-secondary rounded transition-colors"
          >
            Clear
          </button>
        </div>
      )}

      {showUpload && (
        <UploadModal
          currentPath={currentPath}
          onClose={() => setShowUpload(false)}
          onUploaded={onRefresh}
        />
      )}
    </div>
  );
}
