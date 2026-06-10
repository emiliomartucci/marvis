// v1.0.0 - 2026-04-22 - Theme-v2 sidebar for /projects: search + groups + collapse/hide/drawer (PR #9)
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getPrograms } from "@/lib/api";
import type { ProgramInfo, ProjectInfo } from "@/lib/types";

const COLLAPSED_KEY = "marvis-project-collapsed-groups";
const HIDDEN_KEY = "marvis-project-hidden-groups";

function loadSet(key: string): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}

function persistSet(key: string, value: Set<string>) {
  try {
    localStorage.setItem(key, JSON.stringify([...value]));
  } catch {
    // Safari private mode, etc. — in-memory fallback is fine.
  }
}

function statusDotClass(p: ProjectInfo): string {
  if (p.lifecycle === "archived") return "border border-pir-text-muted bg-transparent";
  if (p.status === "active" || p.lifecycle === "active") return "bg-pir-success";
  if (p.status === "paused") return "bg-pir-warning";
  if (p.status === "blocked") return "bg-pir-error";
  return "bg-pir-text-muted";
}

function languageChip(lang: string | null | undefined): string | null {
  if (!lang) return null;
  const v = lang.toLowerCase();
  if (v === "python" || v === "py") return "PY";
  if (v === "typescript" || v === "ts") return "TS";
  if (v === "javascript" || v === "js") return "JS";
  if (v === "markdown" || v === "md") return "MD";
  if (v === "html") return "HT";
  return v.slice(0, 2).toUpperCase();
}

function ProjectsSidebarV2() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeSlug = searchParams.get("slug");

  const [programs, setPrograms] = useState<ProgramInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(loadSet(COLLAPSED_KEY));
  const [hidden, setHidden] = useState<Set<string>>(loadSet(HIDDEN_KEY));
  const [hiddenDrawerOpen, setHiddenDrawerOpen] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    getPrograms({ signal: ctrl.signal })
      .then(setPrograms)
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, []);

  const toggleCollapse = useCallback((name: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      persistSet(COLLAPSED_KEY, next);
      return next;
    });
  }, []);

  const hide = useCallback((name: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      next.add(name);
      persistSet(HIDDEN_KEY, next);
      return next;
    });
  }, []);

  const unhide = useCallback((name: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      next.delete(name);
      persistSet(HIDDEN_KEY, next);
      return next;
    });
  }, []);

  const showAll = useCallback(() => {
    setHidden(new Set());
    persistSet(HIDDEN_KEY, new Set());
  }, []);

  const navigate = useCallback(
    (slug: string) => router.push(`/projects/detail/?slug=${encodeURIComponent(slug)}`),
    [router]
  );

  const lowerSearch = search.trim().toLowerCase();

  const filtered = useMemo(() => {
    return programs
      .map((prog) => ({
        ...prog,
        projects: prog.projects.filter((p) => {
          if (!p.on_server) return false;
          if (!lowerSearch) return true;
          return (
            p.slug.toLowerCase().includes(lowerSearch) ||
            (p.name || "").toLowerCase().includes(lowerSearch)
          );
        }),
      }))
      .filter((prog) => prog.projects.length > 0);
  }, [programs, lowerSearch]);

  const visiblePrograms = filtered.filter((p) => !hidden.has(p.name));

  return (
    <aside
      className="w-60 bg-pir-surface-0 border-r border-pir flex flex-col h-full shrink-0 overflow-hidden"
    >
      {/* Search */}
      <div className="border-b border-pir" style={{ padding: "10px 12px 8px" }}>
        <div className="relative">
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden
            className="absolute text-pir-text-muted"
            style={{ left: 6, top: "50%", transform: "translateY(-50%)" }}
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter projects…"
            className="w-full bg-pir-base border border-pir text-pir-text-secondary placeholder:text-pir-text-muted font-mono outline-none focus:border-pir-accent transition-colors"
            style={{
              fontSize: 11,
              fontWeight: 500,
              padding: "5px 8px 5px 22px",
              borderRadius: 2,
              boxSizing: "border-box",
            }}
          />
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto" style={{ padding: "8px 0 4px" }}>
        {loading && (
          <div className="px-3 py-2 text-pir-text-muted text-[11px]">Loading…</div>
        )}
        {!loading && visiblePrograms.length === 0 && (
          <div className="px-3 py-4 text-pir-text-muted text-[11px]">No projects match.</div>
        )}
        {visiblePrograms.map((prog) => {
          const isCollapsed = collapsed.has(prog.name);
          return (
            <div key={prog.name}>
              <div
                role="button"
                tabIndex={0}
                onClick={() => toggleCollapse(prog.name)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggleCollapse(prog.name);
                  }
                }}
                aria-expanded={!isCollapsed}
                className="flex justify-between items-center cursor-pointer hover:bg-pir-surface-1/60 transition-colors"
                style={{ padding: "10px 12px 6px 12px" }}
              >
                <span
                  className="text-pir-accent relative uppercase"
                  style={{
                    fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                    fontWeight: 600,
                    fontSize: 9,
                    letterSpacing: "0.2em",
                    paddingLeft: 8,
                    lineHeight: 1,
                  }}
                >
                  <span
                    aria-hidden
                    className="absolute bg-pir-accent"
                    style={{
                      left: 0,
                      top: "50%",
                      width: 3,
                      height: 3,
                      transform: "translateY(-50%)",
                    }}
                  />
                  {prog.name}
                </span>
                <span
                  className="flex items-center gap-2 text-pir-text-muted"
                  style={{
                    fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                    fontWeight: 500,
                    fontSize: 9,
                    lineHeight: 1,
                  }}
                >
                  <span>{prog.projects.length}</span>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      hide(prog.name);
                    }}
                    aria-label={`Hide program ${prog.name}`}
                    title="Hide group"
                    className="text-pir-text-muted hover:text-pir-text-primary bg-transparent border-0 p-0 inline-flex items-center justify-center cursor-pointer"
                    style={{
                      width: 13,
                      height: 13,
                      opacity: 0.4,
                      transition: "opacity 150ms",
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.opacity = "0.9")}
                    onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.4")}
                  >
                    <svg
                      width="11"
                      height="11"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden
                    >
                      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z" />
                      <circle cx="12" cy="12" r="3" />
                    </svg>
                  </button>
                  <span
                    aria-hidden
                    className="text-pir-text-muted"
                    style={{
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 9,
                      lineHeight: 1,
                      display: "inline-block",
                      transform: isCollapsed ? "rotate(0deg)" : "rotate(90deg)",
                      transition: "transform 150ms",
                      width: 9,
                      textAlign: "center",
                    }}
                  >
                    ›
                  </span>
                </span>
              </div>
              {!isCollapsed &&
                prog.projects.map((p) => {
                  const active = p.slug === activeSlug;
                  const lang = languageChip(p.language ?? null);
                  return (
                    <button
                      type="button"
                      key={p.slug}
                      onClick={() => navigate(p.slug)}
                      className={`w-full flex items-center gap-2 text-left bg-transparent cursor-pointer transition-colors ${
                        active
                          ? "bg-pir-success/10"
                          : "hover:bg-pir-surface-1/60"
                      }`}
                      style={{
                        padding: "6px 12px",
                        margin: "1px 4px",
                        borderRadius: 2,
                        borderLeft: active ? "2px solid hsl(var(--pir-success))" : "2px solid transparent",
                        border: "0",
                        borderLeftWidth: 2,
                        borderLeftColor: active ? "hsl(var(--pir-success))" : "transparent",
                        borderLeftStyle: "solid",
                      }}
                    >
                      <span
                        className={`shrink-0 rounded-full ${statusDotClass(p)}`}
                        style={{ width: 6, height: 6 }}
                      />
                      <span
                        className={
                          active ? "text-pir-text-primary font-semibold" : "text-pir-text-secondary"
                        }
                        style={{
                          flex: 1,
                          fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                          fontWeight: active ? 600 : 500,
                          fontSize: 12,
                          lineHeight: 1.1,
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                        }}
                      >
                        {p.slug}
                      </span>
                      {lang && (
                        <span
                          className="text-pir-text-tertiary border border-pir shrink-0 uppercase"
                          style={{
                            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                            fontWeight: 500,
                            fontSize: 8.5,
                            letterSpacing: "0.1em",
                            padding: "2px 4px",
                            borderRadius: 2,
                          }}
                        >
                          {lang}
                        </span>
                      )}
                    </button>
                  );
                })}
            </div>
          );
        })}
      </div>

      {/* Hidden drawer */}
      {hidden.size > 0 && (
        <div className="border-t border-pir">
          <button
            type="button"
            onClick={() => setHiddenDrawerOpen((v) => !v)}
            aria-expanded={hiddenDrawerOpen}
            className="text-pir-text-muted flex items-center gap-1.5 bg-transparent border-0 hover:bg-pir-surface-1/60 transition-colors text-left w-full cursor-pointer uppercase"
            style={{
              fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
              fontWeight: 500,
              fontSize: 10,
              letterSpacing: "0.15em",
              padding: "8px 14px",
              lineHeight: 1,
            }}
          >
            <span aria-hidden>◎</span>
            {hidden.size} hidden group{hidden.size === 1 ? "" : "s"}
            <span className="ml-auto" aria-hidden>
              {hiddenDrawerOpen ? "▾" : "▸"}
            </span>
          </button>
          {hiddenDrawerOpen && (
            <div className="flex flex-col gap-0.5 border-t border-pir" style={{ padding: "4px 8px 8px" }}>
              {[...hidden].sort().map((name) => (
                <div
                  key={name}
                  className="flex items-center gap-2 text-pir-text-tertiary"
                  style={{ padding: "4px 8px", fontSize: 11 }}
                >
                  <span className="flex-1 truncate font-mono">{name}</span>
                  <button
                    type="button"
                    onClick={() => unhide(name)}
                    className="text-pir-accent hover:text-pir-text-primary bg-transparent border-0 cursor-pointer uppercase"
                    style={{
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 9,
                      letterSpacing: "0.12em",
                    }}
                  >
                    Unhide
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={showAll}
                className="mt-1 text-pir-text-muted hover:text-pir-accent bg-transparent border-0 cursor-pointer self-start"
                style={{ fontSize: 10, padding: "2px 8px" }}
              >
                Show all
              </button>
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

export default ProjectsSidebarV2;
