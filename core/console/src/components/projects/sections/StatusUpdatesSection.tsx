// v1.0.0 - 2026-04-22 - §2 Feed-style status updates section (PR #9)
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import SectionShell from "./SectionShell";
import SafeMarkdown from "@/components/projects/SafeMarkdown";
import { PermissionGate } from "@/components/PermissionGate";
import {
  getProjectStatusUpdates,
  postProjectStatusUpdate,
} from "@/lib/api";
import type { StatusUpdateFeedItem, StatusUpdateKind } from "@/lib/types";

const KIND_STYLE: Record<StatusUpdateKind, { border: string; label: string; tone: string }> = {
  manual:       { border: "hsl(var(--pir-accent))",          label: "manual",        tone: "text-pir-accent" },
  auto_handoff: { border: "hsl(var(--pir-secondary-bright))", label: "auto · handoff", tone: "text-pir-success" },
  auto_commit:  { border: "hsl(var(--pir-secondary-bright))", label: "auto · commit",  tone: "text-pir-success" },
  ai_summary:   { border: "hsl(300 35% 58%)",                label: "ai · summary",  tone: "text-[hsl(300_35%_68%)]" },
};

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  if (Number.isNaN(then)) return dateStr;
  const diffMs = Math.max(0, now - then);
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min fa`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h fa`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d fa`;
  return new Date(dateStr).toISOString().slice(0, 10);
}

function FilterButton({
  active,
  onClick,
  children,
}: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`transition-colors ${
        active
          ? "text-pir-accent border-pir-accent/50"
          : "text-pir-text-tertiary hover:text-pir-text-primary border-pir"
      }`}
      style={{
        fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
        fontSize: 10,
        fontWeight: 500,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
        padding: "4px 8px",
        border: "1px solid",
        borderRadius: 2,
        background: "transparent",
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

type Filter = "all" | "manual" | "auto";

function StatusUpdatesSection({
  slug,
  textareaRef,
}: {
  slug: string;
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
}) {
  const [items, setItems] = useState<StatusUpdateFeedItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const localRef = useRef<HTMLTextAreaElement>(null);
  const taRef = textareaRef ?? localRef;

  const refresh = useCallback(
    (signal?: AbortSignal) =>
      getProjectStatusUpdates(slug, 20, { signal })
        .then((res) => {
          setItems(res.updates);
          setError(null);
        })
        .catch((err) => {
          if (err?.name !== "AbortError") setError(err?.message || "Load failed");
        })
        .finally(() => setLoading(false)),
    [slug]
  );

  useEffect(() => {
    setLoading(true);
    const ctrl = new AbortController();
    refresh(ctrl.signal);
    return () => ctrl.abort();
  }, [refresh]);

  const handleSubmit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      const content = draft.trim();
      if (!content || submitting) return;
      setSubmitting(true);
      try {
        const created = await postProjectStatusUpdate(slug, content);
        setItems((prev) => [created, ...prev]);
        setDraft("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Post failed");
      } finally {
        setSubmitting(false);
      }
    },
    [draft, slug, submitting]
  );

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      handleSubmit();
    }
  };

  const lastTs = items[0]?.created_at;
  const visibleItems = items.filter((it) => {
    if (filter === "all") return true;
    if (filter === "manual") return it.kind === "manual" || it.kind === "ai_summary";
    return it.kind === "auto_handoff" || it.kind === "auto_commit";
  });

  return (
    <SectionShell
      anchorId="status"
      eyebrow="Status & updates"
      title={
        lastTs ? (
          <>
            Last update ·{" "}
            <span className="font-mono text-[13px] font-medium text-pir-success">
              {timeAgo(lastTs)}
            </span>
          </>
        ) : (
          "Status & updates"
        )
      }
      action={
        <div className="flex gap-1.5">
          <FilterButton active={filter === "all"} onClick={() => setFilter("all")}>
            all
          </FilterButton>
          <FilterButton active={filter === "auto"} onClick={() => setFilter("auto")}>
            auto
          </FilterButton>
          <FilterButton active={filter === "manual"} onClick={() => setFilter("manual")}>
            manual
          </FilterButton>
        </div>
      }
    >
      {/* Manual input (operator+) */}
      <PermissionGate minRole="operator">
        <form
          onSubmit={handleSubmit}
          className="bg-pir-surface-0 border border-pir"
          style={{ borderRadius: 4, padding: "10px 12px" }}
        >
          <textarea
            ref={taRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Aggiorna su questo progetto… supporta markdown, ⌘↵ per salvare"
            className="w-full bg-transparent border-0 outline-0 resize-none text-pir-text-primary placeholder:text-pir-text-muted"
            style={{
              fontFamily: "var(--pir-font-sans, system-ui)",
              fontSize: 13,
              lineHeight: 1.45,
              minHeight: 48,
            }}
            maxLength={8000}
            disabled={submitting}
          />
          <div className="flex items-center justify-between mt-1">
            <span
              className="text-pir-text-muted"
              style={{
                fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                fontSize: 10,
                letterSpacing: "0.08em",
              }}
            >
              Phase 1: manual input · Phase 2 · AI auto-gen
            </span>
            <span className="flex items-center gap-1.5">
              <kbd
                className="bg-pir-surface-2 border border-pir"
                style={{
                  fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                  fontSize: 9,
                  fontWeight: 700,
                  padding: "2px 5px",
                  borderRadius: 2,
                }}
              >
                ⌘↵
              </kbd>
              <button
                type="submit"
                disabled={submitting || !draft.trim()}
                className="bg-pir-accent text-pir-base cursor-pointer border-0 disabled:opacity-50 disabled:cursor-not-allowed"
                style={{
                  fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                  fontWeight: 600,
                  fontSize: 10,
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  padding: "4px 10px",
                  borderRadius: 2,
                }}
              >
                {submitting ? "Saving…" : "Post"}
              </button>
            </span>
          </div>
        </form>
      </PermissionGate>

      {/* Feed */}
      <div className="flex flex-col gap-2 mt-3.5">
        {loading && (
          <div className="text-pir-text-tertiary text-sm px-2 py-4">Loading feed…</div>
        )}
        {!loading && error && (
          <div className="text-pir-error text-sm px-2 py-4">Errore: {error}</div>
        )}
        {!loading && !error && visibleItems.length === 0 && (
          <div className="text-pir-text-tertiary text-sm px-2 py-4">
            Nessun update ancora. Scrivi il primo o aspetta un handoff.
          </div>
        )}
        {visibleItems.map((item) => {
          const style = KIND_STYLE[item.kind];
          return (
            <article
              key={item.id}
              className="bg-pir-surface-0 border border-pir"
              style={{
                borderRadius: 4,
                padding: "10px 14px",
                borderLeft: `2px solid ${style.border}`,
              }}
            >
              <header
                className="flex items-center gap-2 mb-1.5 text-pir-text-muted"
                style={{
                  fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                  fontSize: 10,
                  letterSpacing: "0.08em",
                }}
              >
                <span className={`font-bold uppercase tracking-[0.18em] ${style.tone}`}>
                  {style.label}
                </span>
                {item.author_display && (
                  <span className="text-pir-text-tertiary truncate max-w-[260px]" title={item.author_display}>
                    · {item.author_display}
                  </span>
                )}
                <span className="ml-auto tabular-nums">{timeAgo(item.created_at)}</span>
              </header>
              <div
                className="text-pir-text-secondary"
                style={{
                  fontFamily: "var(--pir-font-sans, system-ui)",
                  fontSize: 12.5,
                  lineHeight: 1.5,
                }}
              >
                <SafeMarkdown content={item.content_md} />
              </div>
            </article>
          );
        })}
      </div>
    </SectionShell>
  );
}

export default StatusUpdatesSection;
