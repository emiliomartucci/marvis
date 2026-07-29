"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { getPrograms } from "@/lib/api";
import type { ProjectInfo } from "@/lib/types";

interface ProjectSelectorModalProps {
  currentSlug?: string | null;
  onSubmit: (slug: string) => void;
  onClose: () => void;
  // Optional pre-render filter (post-deepen M-D8 Opt B). When provided,
  // only projects matching the predicate are visible in the modal.
  filter?: (project: ProjectInfo) => boolean;
}

export default function ProjectSelectorModal({
  currentSlug,
  onSubmit,
  onClose,
  filter,
}: ProjectSelectorModalProps) {
  const [query, setQuery] = useState("");
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Load projects on mount
  useEffect(() => {
    const ctrl = new AbortController();
    getPrograms({ signal: ctrl.signal })
      .then((programs) => {
        const all: ProjectInfo[] = [];
        for (const program of programs) {
          for (const p of program.projects) {
            all.push(p);
          }
        }
        // Sort by program then name
        all.sort((a, b) => {
          const pa = a.program || "";
          const pb = b.program || "";
          if (pa !== pb) return pa.localeCompare(pb);
          return a.name.localeCompare(b.name);
        });
        setProjects(all);
      })
      .catch(() => {});
    return () => ctrl.abort();
  }, []);

  // Autofocus search
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Apply optional pre-filter (M-D8 Opt B) before search filter
  const visible = useMemo(
    () => (filter ? projects.filter(filter) : projects),
    [projects, filter]
  );

  // Filter projects by query
  const filtered = useMemo(() => {
    if (!query.trim()) return visible;
    const q = query.toLowerCase();
    const matches = visible.filter(
      (p) =>
        p.slug.toLowerCase().includes(q) ||
        p.name.toLowerCase().includes(q) ||
        (p.program && p.program.toLowerCase().includes(q))
    );
    // Slug/name matches appear before program-only matches
    matches.sort((a, b) => {
      const aSlug = a.slug.toLowerCase().includes(q) || a.name.toLowerCase().includes(q) ? 0 : 1;
      const bSlug = b.slug.toLowerCase().includes(q) || b.name.toLowerCase().includes(q) ? 0 : 1;
      return aSlug - bSlug;
    });
    return matches;
  }, [query, visible]);

  // Group by program (only when not searching)
  const grouped = useMemo(() => {
    if (query.trim()) return null; // flat list when searching
    const map = new Map<string, ProjectInfo[]>();
    for (const p of filtered) {
      const prog = p.program || "Standalone";
      if (!map.has(prog)) map.set(prog, []);
      map.get(prog)!.push(p);
    }
    return map;
  }, [filtered, query]);

  // Build flat items list for keyboard nav (includes "No project" as index 0)
  const flatItems = useMemo(() => {
    const items: Array<{ type: "clear" } | { type: "project"; project: ProjectInfo }> = [
      { type: "clear" },
    ];
    for (const p of filtered) {
      items.push({ type: "project", project: p });
    }
    return items;
  }, [filtered]);

  // Reset selection when filter changes
  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  // Scroll selected into view
  useEffect(() => {
    if (!listRef.current) return;
    const items = listRef.current.querySelectorAll("[data-project-item]");
    items[selectedIndex]?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  function handleSelect(index: number) {
    const item = flatItems[index];
    if (!item) return;
    if (item.type === "clear") {
      onSubmit("");
    } else {
      onSubmit(item.project.slug);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((i) => Math.min(i + 1, flatItems.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      handleSelect(selectedIndex);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  }

  const statusDotColor: Record<string, string> = {
    active: "bg-pir-success",
    paused: "bg-yellow-500",
    blocked: "bg-pir-error",
    completed: "bg-pir-text-muted",
    not_started: "bg-pir-text-tertiary",
  };

  function renderProjectRow(project: ProjectInfo, itemIndex: number) {
    const isSelected = selectedIndex === itemIndex;
    const isCurrent = project.slug === currentSlug;
    const openCount =
      (project.task_counts?.pending || 0) +
      (project.task_counts?.approved || 0) +
      (project.task_counts?.in_progress || 0);

    return (
      <button
        key={project.slug}
        data-project-item
        className={`w-full text-left px-3 py-2.5 flex items-center gap-2.5 transition-colors ${
          isSelected ? "bg-[rgba(255,255,255,0.06)]" : "hover:bg-[rgba(255,255,255,0.03)]"
        } ${isCurrent ? "border-l-2 border-pir-accent bg-[rgba(255,255,255,0.04)]" : "border-l-2 border-transparent"}`}
        onClick={() => handleSelect(itemIndex)}
        onMouseEnter={() => setSelectedIndex(itemIndex)}
      >
        <span
          className={`w-2 h-2 rounded-full shrink-0 ${statusDotColor[project.status || ""] || "bg-pir-text-tertiary"}`}
          aria-label={project.status || "unknown"}
        />
        <div className="flex flex-col min-w-0 flex-1">
          <span className="text-sm text-pir-text-primary truncate">{project.name}</span>
          {query.trim() && project.program && (
            <span className="text-[11px] text-pir-text-muted truncate">{project.program}</span>
          )}
        </div>
        {openCount > 0 && (
          <span className="text-[11px] bg-[rgba(255,255,255,0.06)] rounded-[10px] px-2 py-0.5 text-pir-text-muted shrink-0">
            {openCount}
          </span>
        )}
      </button>
    );
  }

  // Track current flat index for grouped view
  let flatIndex = 1; // 0 is "No project"

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="bg-pir-surface-0 border border-pir rounded-lg w-[420px] max-w-[calc(100vw-2rem)] max-h-[70vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-selector-title"
        onKeyDown={handleKeyDown}
      >
        {/* Search */}
        <div className="px-3 py-2.5 border-b border-[rgba(255,255,255,0.08)]">
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search projects..."
            className="w-full bg-transparent text-sm text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none"
            role="combobox"
            aria-expanded="true"
            aria-controls="project-list"
          />
        </div>

        {/* List */}
        <div
          ref={listRef}
          id="project-list"
          role="listbox"
          className="overflow-y-auto flex-1"
        >
          {/* No project option */}
          <button
            data-project-item
            className={`w-full text-left px-3 py-2.5 flex items-center gap-2.5 transition-colors border-b border-[rgba(255,255,255,0.06)] ${
              selectedIndex === 0 ? "bg-[rgba(255,255,255,0.06)]" : "hover:bg-[rgba(255,255,255,0.03)]"
            } ${!currentSlug ? "border-l-2 border-pir-accent" : "border-l-2 border-transparent"}`}
            onClick={() => handleSelect(0)}
            onMouseEnter={() => setSelectedIndex(0)}
            role="option"
            aria-selected={selectedIndex === 0}
          >
            <svg className="w-4 h-4 text-pir-text-muted shrink-0" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="8" cy="8" r="6" />
              <line x1="5" y1="5" x2="11" y2="11" />
            </svg>
            <span className="text-sm text-pir-text-secondary italic">No project</span>
          </button>

          {/* Projects - grouped or flat */}
          {grouped
            ? [...grouped.entries()].map(([program, projs]) => (
                <div key={program}>
                  <div className="px-3 py-1.5 text-[11px] uppercase tracking-wide text-pir-text-tertiary">
                    {program}
                  </div>
                  {projs.map((p) => {
                    const idx = flatIndex++;
                    return renderProjectRow(p, idx);
                  })}
                </div>
              ))
            : filtered.map((p) => {
                const idx = flatIndex++;
                return renderProjectRow(p, idx);
              })}

          {filtered.length === 0 && (
            <div className="px-3 py-6 text-center text-sm text-pir-text-muted">
              No projects found
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
