"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { InboxItemSummary, InboxIgnoreReason, InboxStatus, ProgramInfo } from "@/lib/types";
import type { SourceScore } from "@/lib/api";
import { getPrograms } from "@/lib/api";

type Mode = "navigate" | "submenu_top" | "submenu_bottom" | "project_select";

interface ActionViewModalProps {
  currentItem: InboxItemSummary | null;
  currentIndex: number;
  totalItems: number;
  isExhausted: boolean;
  loading: boolean;
  error: string | null;
  toastMessage: string | null;
  content: string | null;
  tldr: string | null;
  tldrLoading: boolean;
  deepResearch: string | null;
  deepResearchLoading: boolean;
  sourceScores: SourceScore[];
  onDecide: (status: InboxStatus, ignoreReason?: InboxIgnoreReason) => void;
  onUndo: () => void;
  onClose: () => void;
  onClearError: () => void;
  onRequestTldr: () => void;
  onRequestDeepResearch: () => void;
  onSaveInPlace: () => void;
}

interface TopMenuOption {
  key: string;
  label: string;
  status: InboxStatus;
  icon?: React.ReactElement;
}

const StarIcon = (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 24 24"
    className="w-3.5 h-3.5 shrink-0 fill-yellow-400"
    aria-hidden="true"
  >
    <path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27z" />
  </svg>
);

const TOP_MENU_OPTIONS: TopMenuOption[] = [
  { key: "1", label: "Salva", status: "saved" },
  { key: "2", label: "Newsletter", status: "newsletter" },
  { key: "3", label: "Spunto / idea", status: "idea" },
  { key: "4", label: "Preferito", status: "preferred", icon: StarIcon },
];

const BOTTOM_MENU_OPTIONS: { key: string; label: string; reason: InboxIgnoreReason }[] = [
  { key: "1", label: "Duplicata", reason: "duplicate" },
  { key: "2", label: "Spam / promo", reason: "spam" },
  { key: "3", label: "Non mi interessa", reason: "not_interested" },
  { key: "4", label: "Non pertinente", reason: "not_relevant" },
];

export function ActionViewModal({
  currentItem,
  currentIndex,
  totalItems,
  isExhausted,
  loading,
  error,
  toastMessage,
  content,
  tldr,
  tldrLoading,
  deepResearch,
  deepResearchLoading,
  sourceScores,
  onDecide,
  onUndo,
  onClose,
  onClearError,
  onRequestTldr,
  onRequestDeepResearch,
  onSaveInPlace,
}: ActionViewModalProps) {
  const [mode, setMode] = useState<Mode>("navigate");
  const [menuIndex, setMenuIndex] = useState(0);
  const [projects, setProjects] = useState<{ slug: string; name: string }[]>([]);
  const [projectFilter, setProjectFilter] = useState("");
  const [projectIndex, setProjectIndex] = useState(0);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const modalRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<number>(0);

  // Reset mode when card changes
  useEffect(() => {
    setMode("navigate");
    setMenuIndex(0);
  }, [currentIndex]);

  // Lock body scroll
  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  // Load projects for idea selector
  useEffect(() => {
    if (mode !== "project_select") return;
    let cancelled = false;
    void (async () => {
      try {
        const programs = await getPrograms();
        const allProjects = programs.flatMap((p: ProgramInfo) =>
          p.projects.map((proj) => ({ slug: proj.slug, name: proj.name }))
        );
        // Deduplicate by slug
        const seen = new Set<string>();
        const unique = allProjects.filter((p) => {
          if (seen.has(p.slug)) return false;
          seen.add(p.slug);
          return true;
        });
        if (!cancelled) {
          setProjects(unique);
          setProjectIndex(0);
          setProjectFilter("");
        }
      } catch {
        if (!cancelled) setProjects([]);
      }
    })();
    return () => { cancelled = true; };
  }, [mode]);

  // Focus search input when entering project_select mode
  useEffect(() => {
    if (mode === "project_select" && searchInputRef.current) {
      searchInputRef.current.focus();
    }
  }, [mode]);

  // Extract domain from item URL for source score lookup
  const currentUrlDomain = useMemo(() => {
    if (!currentItem?.url) return null;
    try {
      const hostname = new URL(currentItem.url).hostname;
      return hostname.replace(/^www\./, "");
    } catch {
      return null;
    }
  }, [currentItem]);

  // Source score for current item (matched by URL domain)
  const currentSourceScore = useMemo(() => {
    if (!currentUrlDomain || sourceScores.length === 0) return null;
    return sourceScores.find((s) => s.source_key === currentUrlDomain) ?? null;
  }, [currentUrlDomain, sourceScores]);

  const filteredProjects = projects.filter((p) => {
    if (!projectFilter) return true;
    const lower = projectFilter.toLowerCase();
    return p.slug.toLowerCase().includes(lower) || p.name.toLowerCase().includes(lower);
  });

  // Keyboard handler
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      // Don't handle if input is focused (project search)
      if (mode === "project_select" && e.target === searchInputRef.current) {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          setMode("submenu_top");
          return;
        }
        if (e.key === "ArrowDown") {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          setProjectIndex((prev) => Math.min(prev + 1, filteredProjects.length - 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          setProjectIndex((prev) => Math.max(prev - 1, 0));
          return;
        }
        if (e.key === "Enter") {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          const selected = filteredProjects[projectIndex];
          if (selected) {
            onDecide("idea");
            setMode("navigate");
          }
          return;
        }
        // Let typing through to input
        return;
      }

      // Debounce rapid presses (300ms)
      const now = Date.now();
      if (now - debounceRef.current < 300 && e.key !== "Escape") {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        return;
      }
      debounceRef.current = now;

      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();

      if (mode === "navigate") {
        switch (e.key) {
          case "ArrowRight":
            onDecide("read");
            break;
          case "ArrowUp":
            setMode("submenu_top");
            setMenuIndex(0);
            break;
          case "ArrowDown":
            setMode("submenu_bottom");
            setMenuIndex(0);
            break;
          case "ArrowLeft":
            onUndo();
            break;
          case "Enter":
            if (currentItem?.url) {
              window.open(currentItem.url, '_blank', 'noopener,noreferrer');
            }
            break;
          case "Escape":
            onClose();
            break;
          case " ":
            onRequestTldr();
            break;
          case "s":
          case "S":
            onSaveInPlace();
            break;
          case "d":
          case "D":
            onRequestDeepResearch();
            break;
          case "i":
          case "I":
            onDecide("ignored", "not_interested");
            break;
        }
      } else if (mode === "submenu_top") {
        switch (e.key) {
          case "ArrowUp":
            setMenuIndex((prev) => Math.max(prev - 1, 0));
            break;
          case "ArrowDown":
            setMenuIndex((prev) => Math.min(prev + 1, TOP_MENU_OPTIONS.length - 1));
            break;
          case "Enter": {
            const option = TOP_MENU_OPTIONS[menuIndex];
            if (option.status === "idea") {
              setMode("project_select");
            } else {
              onDecide(option.status);
              setMode("navigate");
            }
            break;
          }
          case "1":
          case "2":
          case "3":
          case "4": {
            const idx = parseInt(e.key) - 1;
            const option = TOP_MENU_OPTIONS[idx];
            if (option) {
              if (option.status === "idea") {
                setMode("project_select");
              } else {
                onDecide(option.status);
                setMode("navigate");
              }
            }
            break;
          }
          case "Escape":
          case "ArrowLeft":
            setMode("navigate");
            break;
        }
      } else if (mode === "submenu_bottom") {
        switch (e.key) {
          case "ArrowUp":
            setMenuIndex((prev) => Math.max(prev - 1, 0));
            break;
          case "ArrowDown":
            setMenuIndex((prev) => Math.min(prev + 1, BOTTOM_MENU_OPTIONS.length - 1));
            break;
          case "Enter": {
            const option = BOTTOM_MENU_OPTIONS[menuIndex];
            onDecide("ignored", option.reason);
            setMode("navigate");
            break;
          }
          case "1":
          case "2":
          case "3":
          case "4": {
            const idx = parseInt(e.key) - 1;
            const option = BOTTOM_MENU_OPTIONS[idx];
            if (option) {
              onDecide("ignored", option.reason);
              setMode("navigate");
            }
            break;
          }
          case "Escape":
          case "ArrowLeft":
            setMode("navigate");
            break;
        }
      } else if (mode === "project_select") {
        switch (e.key) {
          case "ArrowUp":
            setProjectIndex((prev) => Math.max(prev - 1, 0));
            break;
          case "ArrowDown":
            setProjectIndex((prev) => Math.min(prev + 1, filteredProjects.length - 1));
            break;
          case "Enter": {
            const selected = filteredProjects[projectIndex];
            if (selected) {
              onDecide("idea");
              setMode("navigate");
            }
            break;
          }
          case "Escape":
          case "ArrowLeft":
            setMode("submenu_top");
            break;
        }
      }
    },
    [mode, menuIndex, projectIndex, filteredProjects, currentItem, onDecide, onUndo, onClose, onRequestTldr, onRequestDeepResearch, onSaveInPlace]
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => handleKeyDown(e);
    window.addEventListener("keydown", handler, { capture: true });
    return () => window.removeEventListener("keydown", handler, { capture: true });
  }, [handleKeyDown]);

  // --- Topic badge styles ---
  const topicStyles: Record<string, string> = {
    "ai-news": "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300",
    "ai-products": "bg-fuchsia-100 text-fuchsia-800 dark:bg-fuchsia-900/30 dark:text-fuchsia-300",
    tooling: "bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-300",
    "security-devtools": "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
    "pv-energy": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300",
    "strategy-business": "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
    "policy-politics": "bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-300",
    general: "bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-200",
  };

  return (
    <div
      ref={modalRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="action-view-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onKeyDown={(e) => { e.stopPropagation(); }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {/* Live region for screen readers */}
      <div aria-live="polite" aria-atomic="true" className="sr-only">
        {currentItem
          ? `Card ${currentIndex + 1} of ${totalItems}: ${currentItem.title || "Untitled"}`
          : isExhausted
            ? "All items processed"
            : "Loading"}
      </div>

      <div className="w-full max-w-3xl mx-4">
        {/* Error toast */}
        {error && (
          <div className="mb-3 rounded-lg border border-red-300 bg-red-50 dark:bg-red-900/20 dark:border-red-800 px-4 py-3 text-sm text-red-700 dark:text-red-300 flex items-center justify-between">
            <span>{error}</span>
            <button onClick={onClearError} className="ml-2 text-red-500 hover:text-red-700">
              <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
              </svg>
            </button>
          </div>
        )}

        {/* Main card */}
        <div className="rounded-xl border border-pir bg-pir-surface-0 shadow-2xl overflow-hidden max-h-[80vh] flex flex-col">
          {loading ? (
            <div className="p-12 text-center text-pir-text-muted">
              Loading inbox items...
            </div>
          ) : isExhausted ? (
            <div className="p-12 text-center">
              <div className="text-2xl font-semibold text-pir-text-primary mb-2">
                Tutto letto!
              </div>
              <p className="text-sm text-pir-text-muted mb-6">
                Non ci sono altri item da processare.
              </p>
              <a
                href="/inbox/"
                className="text-sm text-pir-accent hover:underline"
              >
                Vai alla pagina Inbox
              </a>
              <div className="mt-4 text-xs text-pir-text-muted">
                Premi Esc per chiudere
              </div>
            </div>
          ) : currentItem ? (
            <>
              {/* Card header — fixed */}
              <div className="px-6 pt-5 pb-3 flex items-center justify-between shrink-0">
                <div className="flex items-center gap-2">
                  <span className={`px-2.5 py-1 rounded text-xs font-medium ${topicStyles[currentItem.topic] || topicStyles.general}`}>
                    {currentItem.topic}
                  </span>
                  {/* Source score badge */}
                  {(() => {
                    const sourceKey = currentUrlDomain || currentItem.source_label || currentItem.source_type || "unknown";
                    const score = currentSourceScore?.score ?? 0;
                    const scoreColor = score > 0
                      ? "text-emerald-600 dark:text-emerald-400"
                      : score < 0
                        ? "text-red-600 dark:text-red-400"
                        : "text-pir-text-muted";
                    const prefix = score > 0 ? "+" : "";
                    return (
                      <span className={`px-2 py-0.5 rounded bg-pir-surface-1 border border-pir text-xs font-medium ${scoreColor}`}>
                        {sourceKey} {prefix}{score}
                      </span>
                    );
                  })()}
                  {/* read_save bookmark badge */}
                  {currentItem.treatment === "read_save" && (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-pir-surface-1 border border-pir text-xs text-pir-text-muted">
                      <svg xmlns="http://www.w3.org/2000/svg" className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
                        <path d="M5 4a2 2 0 012-2h6a2 2 0 012 2v14l-5-2.5L5 18V4z" />
                      </svg>
                      da salvare
                    </span>
                  )}
                </div>
                <span className="text-xs text-pir-text-muted tabular-nums">
                  {currentIndex + 1} di {totalItems}
                </span>
              </div>

              {/* Title + meta — fixed */}
              <div className="px-6 pb-3 space-y-2 shrink-0">
                <h2
                  id="action-view-title"
                  className="text-lg font-semibold text-pir-text-primary leading-snug break-words"
                >
                  {currentItem.title || "(Senza titolo)"}
                </h2>

                <div className="flex items-center gap-2 text-xs text-pir-text-muted">
                  <span>{currentItem.source_label || currentItem.source_type || "unknown"}</span>
                  {currentItem.sender && (
                    <>
                      <span className="text-pir-border">|</span>
                      <span>{currentItem.sender}</span>
                    </>
                  )}
                  {currentItem.received_at && (
                    <>
                      <span className="text-pir-border">|</span>
                      <span>{formatRelativeTime(currentItem.received_at)}</span>
                    </>
                  )}
                </div>

                {currentItem.program && (
                  <div className="flex items-center gap-2">
                    <span className="px-2 py-0.5 rounded bg-pir-surface-1 border border-pir text-xs text-pir-text-secondary">
                      {currentItem.program}
                    </span>
                    {currentItem.project && (
                      <span className="px-2 py-0.5 rounded bg-pir-surface-1 border border-pir text-xs text-pir-text-secondary">
                        {currentItem.project}
                      </span>
                    )}
                  </div>
                )}

                {/* Link */}
                {currentItem.url && (
                  <a
                    href={currentItem.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-xs text-pir-accent hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
                      <path d="M11 3a1 1 0 100 2h2.586l-6.293 6.293a1 1 0 101.414 1.414L15 6.414V9a1 1 0 102 0V4a1 1 0 00-1-1h-5z" />
                      <path d="M5 5a2 2 0 00-2 2v8a2 2 0 002 2h8a2 2 0 002-2v-3a1 1 0 10-2 0v3H5V7h3a1 1 0 000-2H5z" />
                    </svg>
                    {new URL(currentItem.url).hostname}
                  </a>
                )}
              </div>

              {/* Content area — scrollable */}
              <div className="px-6 pb-4 overflow-y-auto min-h-0 flex-1">
                {tldrLoading && (
                  <div className="mb-3 px-3 py-2 rounded border border-pir-accent/30 bg-pir-accent/5 text-sm text-pir-accent">
                    Generazione TL;DR...
                  </div>
                )}
                {tldr && (
                  <div className="mb-3 space-y-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="px-2 py-0.5 rounded bg-pir-accent/10 text-pir-accent text-[10px] font-semibold uppercase tracking-wider">
                        TL;DR
                      </span>
                    </div>
                    <div className="text-sm text-pir-text-secondary leading-relaxed break-words prose prose-sm dark:prose-invert max-w-none"
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(tldr) }}
                    />
                    <hr className="border-pir my-3" />
                  </div>
                )}
                {deepResearchLoading && (
                  <div className="mb-3 px-3 py-2 rounded border border-amber-400/30 bg-amber-400/5 text-sm text-amber-600 dark:text-amber-400">
                    Approfondimento in corso...
                  </div>
                )}
                {deepResearch && (
                  <div className="mb-3 space-y-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 text-[10px] font-semibold uppercase tracking-wider">
                        Approfondimento
                      </span>
                    </div>
                    {renderDeepResearch(deepResearch)}
                    <hr className="border-pir my-3" />
                  </div>
                )}
                {!tldr && !deepResearch && (
                  <div className="text-sm text-pir-text-secondary leading-relaxed break-words whitespace-pre-wrap">
                    {content || currentItem.snippet || "Nessun contenuto disponibile."}
                  </div>
                )}
              </div>

              {/* Sub-menus — fixed at bottom */}
              {mode === "submenu_top" && (
                <SubMenu
                  title="Salva come:"
                  options={TOP_MENU_OPTIONS.map((o, i) => ({
                    key: o.key,
                    label: o.label,
                    icon: o.icon,
                    active: i === menuIndex,
                  }))}
                  hint="1-4 rapido | Esc annulla"
                />
              )}

              {mode === "submenu_bottom" && (
                <SubMenu
                  title="Perche scarti?"
                  options={BOTTOM_MENU_OPTIONS.map((o, i) => ({
                    key: o.key,
                    label: o.label,
                    active: i === menuIndex,
                  }))}
                  hint="1-4 rapido | Esc annulla"
                />
              )}

              {mode === "project_select" && (
                <div className="border-t border-pir bg-pir-surface-1 px-6 py-4">
                  <div className="text-xs uppercase tracking-wider text-pir-text-muted mb-3">
                    Per quale progetto?
                  </div>
                  <input
                    ref={searchInputRef}
                    type="text"
                    value={projectFilter}
                    onChange={(e) => {
                      setProjectFilter(e.target.value);
                      setProjectIndex(0);
                    }}
                    placeholder="Cerca progetto..."
                    className="w-full bg-pir-surface-0 border border-pir rounded px-3 py-2 text-sm text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent mb-3"
                    onKeyDown={(e) => e.stopPropagation()}
                  />
                  <div className="max-h-40 overflow-auto space-y-0.5">
                    {filteredProjects.length === 0 ? (
                      <div className="text-xs text-pir-text-muted py-2">Nessun progetto trovato</div>
                    ) : (
                      filteredProjects.map((p, i) => (
                        <button
                          key={p.slug}
                          onClick={() => {
                            onDecide("idea");
                            setMode("navigate");
                          }}
                          className={`w-full text-left px-3 py-1.5 rounded text-sm transition-colors ${
                            i === projectIndex
                              ? "bg-pir-accent/10 text-pir-accent"
                              : "text-pir-text-secondary hover:bg-pir-surface-2"
                          }`}
                        >
                          <span className="font-medium">{i + 1}.</span> {p.slug}
                          {p.name !== p.slug && (
                            <span className="ml-2 text-xs text-pir-text-muted">{p.name}</span>
                          )}
                        </button>
                      ))
                    )}
                  </div>
                  <div className="mt-2 text-[10px] text-pir-text-muted">
                    Enter conferma | Esc annulla
                  </div>
                </div>
              )}

              {/* Keyboard hints — fixed at bottom */}
              {mode === "navigate" && (
                <div className="border-t border-pir px-6 py-3 flex items-center justify-between text-[11px] text-pir-text-muted shrink-0">
                  <div className="flex gap-4">
                    <span aria-keyshortcuts="ArrowLeft">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        &larr;
                      </kbd>
                      {" "}indietro
                    </span>
                    <span aria-keyshortcuts="ArrowRight">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        &rarr;
                      </kbd>
                      {" "}letta
                    </span>
                    <span aria-keyshortcuts="s">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        S
                      </kbd>
                      {" "}salva
                    </span>
                    <span aria-keyshortcuts="i">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        I
                      </kbd>
                      {" "}ignora
                    </span>
                  </div>
                  <div className="flex gap-4">
                    <span aria-keyshortcuts="Space">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        Spazio
                      </kbd>
                      {" "}TL;DR
                    </span>
                    <span aria-keyshortcuts="d">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        D
                      </kbd>
                      {" "}approfondisci
                    </span>
                    <span aria-keyshortcuts="Enter">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        Enter
                      </kbd>
                      {" "}apri link
                    </span>
                    <span aria-keyshortcuts="ArrowUp">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        &uarr;
                      </kbd>
                      {" "}top notch
                    </span>
                    <span aria-keyshortcuts="ArrowDown">
                      <kbd className="px-1.5 py-0.5 rounded bg-pir-surface-1 border border-pir font-mono text-[10px]">
                        &darr;
                      </kbd>
                      {" "}scarta
                    </span>
                  </div>
                </div>
              )}
            </>
          ) : null}
        </div>

        {/* Esc hint */}
        <div className="mt-3 text-center text-xs text-white/50">
          Esc = chiudi
        </div>
      </div>

      {/* Feedback toast (save in place, etc.) */}
      {toastMessage && (
        <div
          role="status"
          aria-live="polite"
          className="pointer-events-none fixed bottom-8 left-1/2 -translate-x-1/2 z-[60]"
        >
          <div className="px-4 py-2 rounded-lg bg-pir-surface-2 border border-pir shadow-lg text-sm text-pir-text-primary">
            {toastMessage}
          </div>
        </div>
      )}
    </div>
  );
}

// --- Sub-components ---

function SubMenu({
  title,
  options,
  hint,
}: {
  title: string;
  options: { key: string; label: string; active: boolean; icon?: React.ReactElement }[];
  hint: string;
}) {
  return (
    <div className="border-t border-pir bg-pir-surface-1 px-6 py-4">
      <div className="text-xs uppercase tracking-wider text-pir-text-muted mb-3">
        {title}
      </div>
      <div className="space-y-1">
        {options.map((opt) => (
          <div
            key={opt.key}
            className={`flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
              opt.active
                ? "bg-pir-accent/10 text-pir-accent font-medium"
                : "text-pir-text-secondary"
            }`}
          >
            {opt.active ? (
              <svg xmlns="http://www.w3.org/2000/svg" className="w-3 h-3 shrink-0" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clipRule="evenodd" />
              </svg>
            ) : (
              <span className="w-3 shrink-0" aria-hidden="true" />
            )}
            <span className="font-medium">{opt.key}.</span>
            {opt.icon}
            <span>{opt.label}</span>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[10px] text-pir-text-muted">
        {hint}
      </div>
    </div>
  );
}

// --- Helpers ---

function renderMarkdown(text: string): string {
  // Simple markdown renderer for TL;DR content: bold, links, bullets.
  const blocks: string[] = [];
  let listItems: string[] = [];
  const flushList = () => {
    if (listItems.length === 0) return;
    blocks.push(`<ul class="space-y-1 my-1">${listItems.join("")}</ul>`);
    listItems = [];
  };

  for (const line of text.split("\n")) {
    if (line.startsWith("- ")) {
      listItems.push(
        `<li class="ml-4 list-disc">${renderInlineMarkdownHtml(line.slice(2))}</li>`
      );
      continue;
    }
    flushList();
    blocks.push(renderInlineMarkdownHtml(line));
  }
  flushList();

  return blocks.join("<br>");
}

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

interface InlineHtmlToken {
  start: number;
  end: number;
  html: string;
}

function firstInlineHtmlToken(
  left: InlineHtmlToken | null,
  right: InlineHtmlToken | null
): InlineHtmlToken | null {
  if (!left) return right;
  if (!right) return left;
  return left.start <= right.start ? left : right;
}

function findBoldHtmlToken(text: string, cursor: number): InlineHtmlToken | null {
  const start = text.indexOf("**", cursor);
  if (start < 0) return null;
  const markerEnd = text.indexOf("**", start + 2);
  if (markerEnd < 0) {
    return { start, end: text.length, html: escapeHtml(text.slice(start)) };
  }

  const end = markerEnd + 2;
  const label = text.slice(start + 2, markerEnd).trim();
  return {
    start,
    end,
    html: label
      ? `<strong>${escapeHtml(label)}</strong>`
      : escapeHtml(text.slice(start, end)),
  };
}

function findLinkHtmlToken(text: string, cursor: number): InlineHtmlToken | null {
  const start = text.indexOf("[", cursor);
  if (start < 0) return null;
  const labelEnd = text.indexOf("](", start);
  const markerEnd = labelEnd >= 0 ? text.indexOf(")", labelEnd + 2) : -1;
  if (labelEnd < 0 || markerEnd < 0) {
    return { start, end: start + 1, html: escapeHtml(text[start]) };
  }

  const label = text.slice(start + 1, labelEnd);
  const url = text.slice(labelEnd + 2, markerEnd);
  const end = markerEnd + 1;
  if (!label || (!url.startsWith("https://") && !url.startsWith("http://"))) {
    return { start, end, html: escapeHtml(text.slice(start, end)) };
  }

  return {
    start,
    end,
    html: `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="text-pir-accent hover:underline">${escapeHtml(label)}</a>`,
  };
}

function findInlineHtmlToken(text: string, cursor: number): InlineHtmlToken | null {
  return firstInlineHtmlToken(
    findBoldHtmlToken(text, cursor),
    findLinkHtmlToken(text, cursor)
  );
}

function renderInlineMarkdownHtml(text: string): string {
  let html = "";
  let cursor = 0;

  while (cursor < text.length) {
    const token = findInlineHtmlToken(text, cursor);
    if (!token) {
      html += escapeHtml(text.slice(cursor));
      break;
    }

    html += escapeHtml(text.slice(cursor, token.start));
    html += token.html;
    cursor = token.end;
  }

  return html;
}

interface BoldRange {
  start: number;
  end: number;
  label: string;
}

function findBoldRange(text: string, cursor: number): BoldRange | null {
  const start = text.indexOf("**", cursor);
  if (start < 0) return null;
  const markerEnd = text.indexOf("**", start + 2);
  if (markerEnd < 0) return null;
  return {
    start,
    end: markerEnd + 2,
    label: text.slice(start + 2, markerEnd).trim(),
  };
}

interface DeepResearchStructured {
  context?: string;
  signals?: { text: string; source: string }[];
  movers?: { name: string; url: string; what: string }[];
  reddit_hn?: string;
  projects?: string[];
}

export function renderInlineMarkdown(text: string): React.ReactNode {
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  let key = 0;

  while (cursor < text.length) {
    const boldRange = findBoldRange(text, cursor);
    if (!boldRange) {
      nodes.push(text.slice(cursor));
      break;
    }
    if (boldRange.start > cursor) {
      nodes.push(text.slice(cursor, boldRange.start));
    }
    if (!boldRange.label) {
      nodes.push(text.slice(boldRange.start, boldRange.end));
      cursor = boldRange.end;
      continue;
    }
    nodes.push(
      <strong key={`bold-${key++}`} className="font-semibold text-pir-text-primary">
        {boldRange.label}
      </strong>
    );
    cursor = boldRange.end;
  }

  return nodes.length > 0 ? nodes : text;
}

export function renderDeepResearch(raw: string): React.ReactElement {
  // Try structured JSON first, fall back to markdown
  let structured: DeepResearchStructured | null = null;
  try {
    let cleanRaw = raw.trim();
    if (cleanRaw.startsWith("```")) {
      cleanRaw = cleanRaw.slice(cleanRaw.indexOf("\n") + 1);
      if (cleanRaw.trimEnd().endsWith("```")) cleanRaw = cleanRaw.trimEnd().slice(0, -3).trimEnd();
    }
    const parsed = JSON.parse(cleanRaw);
    if (parsed && typeof parsed === "object" && "context" in parsed) {
      structured = parsed as DeepResearchStructured;
    }
  } catch {
    // Not JSON, use markdown fallback
  }

  if (!structured) {
    return (
      <div
        className="text-sm text-pir-text-secondary leading-relaxed break-words prose prose-sm dark:prose-invert max-w-none"
        dangerouslySetInnerHTML={{ __html: renderMarkdown(raw) }}
      />
    );
  }

  return (
    <div className="space-y-3">
      {/* Context */}
      {structured.context && (
        <p className="text-sm text-pir-text-secondary leading-relaxed">
          {renderInlineMarkdown(structured.context)}
        </p>
      )}

      {/* Signals */}
      {structured.signals && structured.signals.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {structured.signals.map((sig, i) => (
            <span
              key={i}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-pir-surface-1 border border-pir text-[10px] font-mono text-pir-text-muted"
            >
              <span className="text-pir-text-secondary font-medium">{sig.source}:</span> {sig.text}
            </span>
          ))}
        </div>
      )}

      {/* Movers */}
      {structured.movers && structured.movers.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {structured.movers.map((mover, i) => (
            <a
              key={i}
              href={mover.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-400 text-[10px] font-mono hover:bg-indigo-500/30 transition-colors"
              title={mover.what}
            >
              {mover.name}
              <span className="text-indigo-300/60">{mover.what}</span>
            </a>
          ))}
        </div>
      )}

      {/* Reddit/HN */}
      {structured.reddit_hn && (
        <p className="text-xs text-pir-text-muted italic">
          Reddit/HN: {renderInlineMarkdown(structured.reddit_hn)}
        </p>
      )}

      {/* Projects */}
      {structured.projects && structured.projects.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {structured.projects.map((slug) => (
            <span
              key={slug}
              className="px-1.5 py-0.5 rounded bg-pir-surface-2 text-[10px] text-pir-text-muted font-mono"
            >
              {slug}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return dateStr;
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return "adesso";
  if (diffMins < 60) return `${diffMins}m fa`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h fa`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}g fa`;
  return date.toLocaleDateString();
}
