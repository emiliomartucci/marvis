"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useSemanticSearch } from "@/hooks/useSemanticSearch";
import type { SearchHit } from "@/lib/api";

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

interface ProjectSearchProps {
  slug: string;
}

export default function ProjectSearch({ slug }: ProjectSearchProps) {
  const [focused, setFocused] = useState(false);
  const router = useRouter();
  const { query, search, clear, allHits, loading, error, results } =
    useSemanticSearch(300);

  // Filter results to current project only
  const projectHits = allHits.filter((hit) => hit.project === slug);

  return (
    <div className="relative w-56">
      {loading ? (
        <svg
          className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-pir-accent animate-spin pointer-events-none"
          fill="none"
          viewBox="0 0 24 24"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
          />
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
        value={query}
        onChange={(e) => search(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setTimeout(() => setFocused(false), 200)}
        placeholder="Search in project..."
        className="w-full pl-7 pr-2 py-1 text-caption bg-pir-surface-1 border border-pir rounded text-pir-text-secondary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent"
      />

      {focused && query.trim().length > 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-pir-surface-0 border border-pir rounded-lg shadow-xl z-50 max-h-64 overflow-y-auto">
          {error && (
            <div className="px-3 py-2 text-caption text-pir-error text-center">
              {error}
            </div>
          )}
          {!loading && !error && projectHits.length === 0 && results && (
            <div className="px-3 py-2 text-caption text-pir-text-muted text-center">
              No results in {slug}
            </div>
          )}
          {projectHits.map((hit, i) => (
            <button
              key={`${hit.doc_type}-${hit.doc_id}-${i}`}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                router.push(urlFor(hit));
                clear();
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
              </div>
              <span className="text-[10px] text-pir-text-muted shrink-0 opacity-0 group-hover:opacity-100">
                {Math.round(hit.score * 100)}%
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
