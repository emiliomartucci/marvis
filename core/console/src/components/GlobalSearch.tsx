"use client";

// v1.1.0 - 2026-04-22 - Add Files tab (path + content search), fuzzy path matching (PR #10)
import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import { globalSearch, type SearchHit, type SearchResponse } from "@/lib/api";

type Tab = "all" | "tasks" | "projects" | "files" | "handoffs";

function urlFor(hit: SearchHit): string {
  switch (hit.doc_type) {
    case "task":
      return `/triage/?task=${encodeURIComponent(hit.doc_id)}`;
    case "project":
      return `/projects/detail/?slug=${encodeURIComponent(hit.project)}`;
    case "file": {
      // Navigate to parent directory with file highlighted
      const parts = (hit.path ?? "").split("/");
      const dir = parts.slice(0, -1).join("/");
      const file = parts[parts.length - 1] ?? "";
      return `/finder/?path=${encodeURIComponent(dir)}&highlight=${encodeURIComponent(file)}`;
    }
    case "handoff":
      return `/projects/detail/?slug=${encodeURIComponent(hit.project)}&highlight=${encodeURIComponent(hit.doc_id)}`;
  }
}

const DOC_TYPE_LABEL: Record<SearchHit["doc_type"], string> = {
  task: "Task",
  project: "Project",
  file: "File",
  handoff: "Handoff",
};

function SearchResult({ hit, onSelect }: { hit: SearchHit; onSelect: () => void }) {
  const router = useRouter();

  const handleClick = () => {
    router.push(urlFor(hit));
    onSelect();
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      className="w-full px-3 py-2 text-left hover:bg-pir-surface-1 transition-colors flex items-start gap-2 group"
    >
      <span className="text-caption text-pir-text-muted mt-0.5 shrink-0 w-14">
        {DOC_TYPE_LABEL[hit.doc_type]}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-label text-pir-text-primary truncate group-hover:text-pir-accent">
          {hit.title}
        </div>
        <div className="text-caption text-pir-text-muted truncate">{hit.project}</div>
      </div>
      <span className="text-caption text-pir-text-muted shrink-0 opacity-0 group-hover:opacity-100">
        {Math.round(hit.score * 100)}%
      </span>
    </button>
  );
}

export function GlobalSearch() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("all");
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const closeSearch = useCallback(() => {
    setOpen(false);
    setQuery("");
    setResults(null);
    setError(null);
    setTab("all");
  }, []);

  // Keyboard shortcut: Cmd/Ctrl+K
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen(true);
        setTimeout(() => inputRef.current?.focus(), 0);
      }
      if (e.key === "Escape") {
        closeSearch();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [closeSearch]);

  const handleInput = (value: string) => {
    setQuery(value);

    // Cancel previous debounce + request
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (abortRef.current) abortRef.current.abort();

    if (!value.trim()) {
      setResults(null);
      setLoading(false);
      return;
    }

    // setLoading BEFORE setTimeout so spinner shows during debounce+network
    setLoading(true);
    setError(null);

    debounceRef.current = setTimeout(async () => {
      const controller = new AbortController();
      abortRef.current = controller;

      // canceled flag prevents setState on stale requests
      let canceled = false;
      controller.signal.addEventListener("abort", () => { canceled = true; });

      try {
        const data = await globalSearch(value.trim(), { signal: controller.signal });
        if (!canceled) {
          setResults(data);
          setLoading(false);
        }
      } catch (err) {
        if (!canceled) {
          if ((err as Error).name !== "AbortError") {
            setError("Search unavailable");
          }
          setLoading(false);
        }
      }
    }, 300);
  };

  // Fuzzy-path match score for Files tab (simple subsequence + substring bonus).
  // When semantic backend returns no file hits (cold cache, short query), we
  // still want to surface path matches derived from the `path` field on each
  // file hit. We keep the logic small and client-side — no extra endpoint.
  const filesTabHits: SearchHit[] = useMemo(() => {
    if (!results) return [];
    const base = results.files;
    if (!query.trim()) return base;
    const q = query.trim().toLowerCase();
    // Boost hits whose path contains the query verbatim or whose path segments
    // match as a subsequence. Falls back to the backend ordering when score
    // differences are negligible.
    return [...base]
      .map((h) => {
        const path = (h.path ?? h.title ?? "").toLowerCase();
        let bonus = 0;
        if (path.includes(q)) bonus += 0.12;
        // subsequence bonus
        let i = 0;
        for (const ch of path) {
          if (i < q.length && ch === q[i]) i++;
        }
        if (i === q.length) bonus += 0.05;
        return { ...h, __boost: bonus };
      })
      .sort((a, b) => {
        const sa = (a.score ?? 0) + (a as unknown as { __boost: number }).__boost;
        const sb = (b.score ?? 0) + (b as unknown as { __boost: number }).__boost;
        return sb - sa;
      });
  }, [results, query]);

  const allHits: SearchHit[] = useMemo(() => {
    if (!results) return [];
    switch (tab) {
      case "tasks":
        return results.tasks;
      case "projects":
        return results.projects;
      case "files":
        return filesTabHits;
      case "handoffs":
        return results.handoffs;
      case "all":
      default:
        // Original behavior: files + handoffs (no tasks/projects in global default).
        return [...results.files, ...results.handoffs];
    }
  }, [results, tab, filesTabHits]);

  const tabCounts = useMemo(() => {
    if (!results) return { all: 0, tasks: 0, projects: 0, files: 0, handoffs: 0 };
    return {
      all: results.files.length + results.handoffs.length,
      tasks: results.tasks.length,
      projects: results.projects.length,
      files: results.files.length,
      handoffs: results.handoffs.length,
    };
  }, [results]);

  return (
    <>
      {/* Search trigger button */}
      <button
        type="button"
        onClick={() => {
          setOpen(true);
          setTimeout(() => inputRef.current?.focus(), 0);
        }}
        className="hidden md:flex items-center gap-2 px-2 py-1 text-caption text-pir-text-muted bg-pir-surface-1 hover:bg-pir-surface-2 rounded border border-pir transition-colors"
        aria-label="Search (⌘K)"
      >
        <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clipRule="evenodd" />
        </svg>
        <span>Search</span>
        <kbd className="text-pir-text-muted opacity-60 font-sans">⌘K</kbd>
      </button>

      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/20"
          onClick={closeSearch}
        />
      )}

      {/* Dropdown panel */}
      {open && (
        <div className="fixed top-14 left-1/2 -translate-x-1/2 z-50 w-full max-w-xl bg-pir-surface-0 border border-pir rounded-lg shadow-xl overflow-hidden">
          {/* Input */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-pir">
            {loading ? (
              <svg className="w-4 h-4 text-pir-accent animate-spin shrink-0" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            ) : (
              <svg className="w-4 h-4 text-pir-text-muted shrink-0" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clipRule="evenodd" />
              </svg>
            )}
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => handleInput(e.target.value)}
              placeholder="Search tasks, projects, files, handoffs..."
              className="flex-1 bg-transparent text-label text-pir-text-primary placeholder:text-pir-text-muted outline-none"
              autoComplete="off"
            />
            <button
              type="button"
              onClick={closeSearch}
              className="text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
            >
              Esc
            </button>
          </div>

          {/* Tab switcher */}
          <div
            className="flex items-center gap-0 px-2 py-1 border-b border-pir text-pir-text-muted"
            style={{ fontFamily: "var(--pir-font-mono, ui-monospace, monospace)", fontSize: 10 }}
            role="tablist"
            aria-label="Search filters"
          >
            {(["all", "tasks", "projects", "files", "handoffs"] as Tab[]).map((t) => {
              const active = t === tab;
              return (
                <button
                  key={t}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => setTab(t)}
                  className={`px-2 py-1 uppercase transition-colors ${
                    active
                      ? "text-pir-accent"
                      : "text-pir-text-tertiary hover:text-pir-text-secondary"
                  }`}
                  style={{ letterSpacing: "0.14em", fontWeight: active ? 600 : 500 }}
                >
                  {t}
                  {results && tabCounts[t] > 0 && (
                    <span className="ml-1 text-pir-text-muted tabular-nums" style={{ fontSize: 9 }}>
                      {tabCounts[t]}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {/* Results */}
          <div className="max-h-80 overflow-y-auto">
            {error && (
              <div className="px-3 py-3 text-caption text-pir-error text-center">{error}</div>
            )}
            {!loading && !error && results && allHits.length === 0 && (
              <div className="px-3 py-3 text-caption text-pir-text-muted text-center">
                No results for &ldquo;{query}&rdquo;
              </div>
            )}
            {!loading && !error && !results && !query && (
              <div className="px-3 py-3 text-caption text-pir-text-muted text-center">
                Type to search across all your content
              </div>
            )}
            {allHits.map((hit, i) => (
              <SearchResult key={`${hit.doc_type}-${hit.doc_id}-${i}`} hit={hit} onSelect={closeSearch} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}
