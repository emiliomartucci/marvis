"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { getProjectDocs } from "@/lib/api";
import type { DocEntry } from "@/lib/types";
import FileViewerModal from "./FileViewerModal";

const CATEGORY_COLORS: Record<string, string> = {
  plan: "bg-pir-accent/15 text-pir-accent",
  solution: "bg-pir-success/15 text-pir-success",
  brainstorm: "bg-pir-warning/15 text-pir-warning",
  design: "bg-pir-purple/15 text-pir-purple",
  analysis: "bg-pir-info/15 text-pir-info",
};

const FILTER_OPTIONS = ["all", "plan", "brainstorm", "solution", "design"] as const;
type FilterType = (typeof FILTER_OPTIONS)[number];

const DATE_FILTERS = [
  { id: "all-time", label: "all time" },
  { id: "this-month", label: "this month" },
  { id: "last-30", label: "last 30d" },
  { id: "older", label: "older" },
] as const;
type DateFilter = (typeof DATE_FILTERS)[number]["id"];

function getCategoryColor(category: string): string {
  return CATEGORY_COLORS[category] || "bg-pir-surface-0 text-pir-text-muted";
}

function starredKey(slug: string) {
  return `starred-docs:${slug}`;
}

function loadStarred(slug: string): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const v = localStorage.getItem(starredKey(slug));
    return v ? new Set(JSON.parse(v)) : new Set();
  } catch {
    return new Set();
  }
}

function saveStarred(slug: string, starred: Set<string>) {
  localStorage.setItem(starredKey(slug), JSON.stringify([...starred]));
}

function matchesDateFilter(date: string | null, filter: DateFilter): boolean {
  if (filter === "all-time") return true;
  if (!date) return filter === "older";
  const d = new Date(date);
  const now = new Date();
  const msPerDay = 86400000;
  const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);
  if (filter === "this-month") return d >= startOfMonth;
  if (filter === "last-30") return d >= new Date(now.getTime() - 30 * msPerDay);
  if (filter === "older") return d < new Date(now.getTime() - 30 * msPerDay);
  return true;
}

function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      width="14" height="14" viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round"
    >
      <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />
    </svg>
  );
}

export default function DocsTab({ slug }: { slug: string }) {
  const [docs, setDocs] = useState<DocEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewingFile, setViewingFile] = useState<DocEntry | null>(null);
  const [filter, setFilter] = useState<FilterType>("all");
  const [dateFilter, setDateFilter] = useState<DateFilter>("all-time");
  const [search, setSearch] = useState("");
  const [starredOnly, setStarredOnly] = useState(false);
  const [starred, setStarred] = useState<Set<string>>(new Set());

  useEffect(() => {
    setStarred(loadStarred(slug));
  }, [slug]);

  useEffect(() => {
    const controller = new AbortController();
    getProjectDocs(slug, { signal: controller.signal })
      .then(setDocs)
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [slug]);

  const toggleStar = useCallback((filename: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setStarred((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      saveStarred(slug, next);
      return next;
    });
  }, [slug]);

  const filtered = docs.filter((d) => {
    if (filter !== "all" && d.category !== filter) return false;
    if (!matchesDateFilter(d.date, dateFilter)) return false;
    if (starredOnly && !starred.has(d.filename)) return false;
    if (search) {
      const q = search.toLowerCase();
      const title = (d.title || "").toLowerCase();
      const filename = d.filename.toLowerCase();
      if (!title.includes(q) && !filename.includes(q)) return false;
    }
    return true;
  });

  // Bug 1 (hotfix 2026-04-22): sort docs by ISO date desc (null dates at end).
  // Source order from the API is not guaranteed chronological so we sort here
  // to keep the modal and the legacy tab view consistent.
  const sortedDocs = useMemo(() => {
    return [...filtered].sort((a, b) => {
      if (!a.date && !b.date) return 0;
      if (!a.date) return 1;
      if (!b.date) return -1;
      return b.date.localeCompare(a.date);
    });
  }, [filtered]);

  const categoryCounts = docs.reduce<Record<string, number>>((acc, d) => {
    const cat = d.category || "other";
    acc[cat] = (acc[cat] || 0) + 1;
    return acc;
  }, {});

  if (loading) return <div className="text-pir-text-muted text-body p-4">Loading docs...</div>;
  if (docs.length === 0) return <div className="text-pir-text-muted text-body p-4">No plans or solutions found.</div>;

  return (
    <>
      {/* Filter bar — row 1: search + category + starred */}
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <input
          type="text"
          placeholder="Search docs..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="bg-pir-surface-1 border border-pir rounded px-2.5 py-1 text-caption text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent transition-colors w-44"
        />
        <div className="flex gap-1">
          {FILTER_OPTIONS.map((opt) => (
            <button
              key={opt}
              onClick={() => setFilter(opt)}
              className={`px-2 py-1 text-caption rounded transition-colors ${
                filter === opt
                  ? opt === "all"
                    ? "bg-pir-surface-2 text-pir-text-primary"
                    : getCategoryColor(opt)
                  : "text-pir-text-muted hover:text-pir-text-secondary"
              }`}
            >
              {opt}
              {opt !== "all" && categoryCounts[opt] ? (
                <span className="ml-1 text-[10px] opacity-60">{categoryCounts[opt]}</span>
              ) : opt === "all" ? (
                <span className="ml-1 text-[10px] opacity-60">{docs.length}</span>
              ) : null}
            </button>
          ))}
        </div>
        {/* Starred toggle */}
        <button
          onClick={() => setStarredOnly((v) => !v)}
          className={`ml-auto flex items-center gap-1 px-2 py-1 text-caption rounded transition-colors ${
            starredOnly
              ? "text-pir-warning bg-pir-warning/10"
              : "text-pir-text-muted hover:text-pir-warning"
          }`}
          title="Show starred only"
        >
          <StarIcon filled={starredOnly} />
          {starred.size > 0 && <span className="text-[10px]">{starred.size}</span>}
        </button>
      </div>

      {/* Filter bar — row 2: date presets */}
      <div className="flex gap-1 mb-3">
        {DATE_FILTERS.map((df) => (
          <button
            key={df.id}
            onClick={() => setDateFilter(df.id)}
            className={`px-2 py-0.5 text-[10px] font-mono rounded transition-colors ${
              dateFilter === df.id
                ? "bg-pir-surface-2 text-pir-text-secondary"
                : "text-pir-text-muted hover:text-pir-text-secondary"
            }`}
          >
            {df.label}
          </button>
        ))}
      </div>

      {/* Docs list */}
      <div className="space-y-1">
        {sortedDocs.map((d) => {
          const isStarred = starred.has(d.filename);
          return (
            <button
              key={d.filename}
              onClick={() => setViewingFile(d)}
              className="w-full text-left px-3 py-2 bg-pir-surface-1 border border-pir rounded hover:border-pir-accent transition-colors cursor-pointer group"
            >
              <div className="flex items-center gap-2">
                {/* Star */}
                <span
                  onClick={(e) => toggleStar(d.filename, e)}
                  className={`shrink-0 transition-colors ${
                    isStarred ? "text-pir-warning" : "text-pir-text-muted/30 group-hover:text-pir-text-muted/60"
                  }`}
                >
                  <StarIcon filled={isStarred} />
                </span>

                {/* Date */}
                {d.date && (
                  <span className="text-[10px] text-pir-text-muted font-mono shrink-0">{d.date}</span>
                )}

                {/* Category */}
                {d.category && (
                  <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded shrink-0 ${getCategoryColor(d.category)}`}>
                    {d.category}
                  </span>
                )}

                {/* Title */}
                <span className="text-[12px] text-pir-text-secondary line-clamp-1 min-w-0">
                  {d.title || d.filename.split("/").pop()?.replace(/\.md$/, "")}
                </span>
              </div>
            </button>
          );
        })}
        {sortedDocs.length === 0 && (
          <div className="text-caption text-pir-text-muted py-4 text-center">
            No docs matching filter
          </div>
        )}
      </div>

      {viewingFile && (
        <FileViewerModal
          slug={slug}
          filePath={viewingFile.filename}
          filename={viewingFile.filename.split("/").pop() || viewingFile.filename}
          onClose={() => setViewingFile(null)}
        />
      )}
    </>
  );
}
