"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchTriageCounters,
  listIngestHistory,
  type TriageCounters,
} from "@/lib/api";
import type { IngestHistoryDecision, IngestHistoryEntry } from "@/lib/types";
import { formatBytes } from "./format";

type CounterState =
  | { status: "loading"; counters: TriageCounters }
  | { status: "ready"; counters: TriageCounters }
  | { status: "error"; counters: TriageCounters };

const ZERO_COUNTERS: TriageCounters = { auto: 0, manual: 0 };

const TOOLTIP_TEXT =
  "auto: decisioni automatiche Ingestor\nmanual: approvati via Triage UI";

type HistoryFilter = IngestHistoryDecision | "all";

const HISTORY_FILTERS: Array<{ value: HistoryFilter; label: string }> = [
  { value: "all", label: "Tutti" },
  { value: "auto_approved", label: "Auto OK" },
  { value: "auto_rejected", label: "Auto reject" },
  { value: "manual_approved", label: "Manual OK" },
  { value: "manual_rejected", label: "Manual reject" },
  { value: "parse_error", label: "Parse error" },
  { value: "skipped", label: "Ignorati" },
];

const DECISION_LABEL: Record<IngestHistoryDecision, string> = {
  auto_approved: "AUTO OK",
  auto_rejected: "AUTO REJECT",
  manual_approved: "MANUAL OK",
  manual_rejected: "MANUAL REJECT",
  parse_error: "PARSE ERROR",
  skipped: "IGNORATO",
};

const DECISION_CLASS: Record<IngestHistoryDecision, string> = {
  auto_approved: "border-pir-success/40 bg-pir-success/10 text-pir-success",
  auto_rejected: "border-pir-warning/40 bg-pir-warning/10 text-pir-warning",
  manual_approved: "border-pir-success/30 bg-pir-success/5 text-pir-success",
  manual_rejected: "border-pir-error/40 bg-pir-error/10 text-pir-error",
  parse_error: "border-pir-error/40 bg-pir-error/10 text-pir-error",
  skipped: "border-pir-strong bg-pir-surface-2 text-pir-text-tertiary",
};

export function AutoApprovedCounter() {
  const [state, setState] = useState<CounterState>({
    status: "loading",
    counters: ZERO_COUNTERS,
  });
  const [drawerOpen, setDrawerOpen] = useState(false);

  const refetch = useCallback(async () => {
    try {
      const counters = await fetchTriageCounters({ today: true });
      setState({ status: "ready", counters });
    } catch {
      setState((current) => ({ status: "error", counters: current.counters }));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const guardedRefetch = async () => {
      if (cancelled) return;
      await refetch();
    };
    void guardedRefetch();
    const handler = () => {
      void guardedRefetch();
    };
    window.addEventListener("marvisx:ingest_changed", handler);
    return () => {
      cancelled = true;
      window.removeEventListener("marvisx:ingest_changed", handler);
    };
  }, [refetch]);

  const { auto, manual } = state.counters;

  return (
    <>
      <button
        type="button"
        onClick={() => setDrawerOpen(true)}
        title={TOOLTIP_TEXT}
        aria-label={`${auto} auto-approvati e ${manual} manuali oggi`}
        className="inline-flex h-8 items-center gap-2 rounded-sm border border-pir-success/35 bg-pir-success/10 px-3 font-mono text-caption text-pir-success transition-colors hover:border-pir-success/60 focus:border-pir-accent focus:outline-none"
      >
        <span className="text-label font-bold tabular-nums">{auto}</span>
        <span className="text-pir-text-tertiary">auto</span>
        <span className="text-pir-text-muted">·</span>
        <span className="text-label font-bold tabular-nums">{manual}</span>
        <span className="text-pir-text-tertiary">manual oggi</span>
        {state.status === "error" && <span className="text-pir-warning">stale</span>}
      </button>
      {drawerOpen && (
        <AutoApprovedHistoryDrawer
          counters={state.counters}
          onClose={() => setDrawerOpen(false)}
        />
      )}
    </>
  );
}

function AutoApprovedHistoryDrawer({
  counters,
  onClose,
}: {
  counters: TriageCounters;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState<HistoryFilter>("all");
  const [todayOnly, setTodayOnly] = useState(true);
  const [historyState, setHistoryState] = useState<
    | { status: "loading"; items: IngestHistoryEntry[] }
    | { status: "ready"; items: IngestHistoryEntry[] }
    | { status: "error"; items: IngestHistoryEntry[] }
  >({ status: "loading", items: [] });

  // P1.5.E1: ESC keyboard close (post-deepen H-D4 cleanup return + capture:true)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation(); // H-D5: NOT stopImmediatePropagation
        onClose();
      }
    };
    window.addEventListener("keydown", handler, { capture: true });
    return () => window.removeEventListener("keydown", handler, { capture: true });
  }, [onClose]);

  useEffect(() => {
    const controller = new AbortController();
    async function loadHistory() {
      setHistoryState((current) => ({
        status: current.items.length > 0 ? "ready" : "loading",
        items: current.items,
      }));
      try {
        const items = await listIngestHistory({
          decision: filter,
          today: todayOnly,
          limit: 120,
          signal: controller.signal,
        });
        setHistoryState({ status: "ready", items });
      } catch {
        if (!controller.signal.aborted) {
          setHistoryState((current) => ({ status: "error", items: current.items }));
        }
      }
    }

    void loadHistory();
    const handler = () => {
      void loadHistory();
    };
    window.addEventListener("marvisx:ingest_changed", handler);
    return () => {
      controller.abort();
      window.removeEventListener("marvisx:ingest_changed", handler);
    };
  }, [filter, todayOnly]);

  return (
    <div
      className="fixed inset-0 z-40 bg-pir-base/60"
      role="presentation"
      onClick={(e) => {
        // P1.5.E1: backdrop close (target === currentTarget per safe inner clicks)
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Storico decisioni ingest"
        className="ml-auto flex h-full w-full max-w-[520px] flex-col border-l border-pir bg-pir-surface-0"
      >
        <header className="flex items-center justify-between border-b border-pir px-4 py-3">
          <div>
            <h2 className="font-display text-heading text-pir-text-primary">
              Decisioni ingest
            </h2>
            <p className="mt-1 font-mono text-caption text-pir-text-tertiary">
              {counters.auto} auto · {counters.manual} manual oggi
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="h-7 rounded-sm border border-pir bg-transparent px-2 font-mono text-caption uppercase text-pir-text-tertiary hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
          >
            Chiudi
          </button>
        </header>

        <div className="border-b border-pir px-4 py-3">
          <div className="flex flex-wrap gap-1.5">
            {HISTORY_FILTERS.map((option) => {
              const active = option.value === filter;
              return (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setFilter(option.value)}
                  className={`h-7 rounded-sm border px-2 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] transition-colors focus:border-pir-accent focus:outline-none ${
                    active
                      ? "border-pir-accent bg-pir-accent/12 text-pir-accent"
                      : "border-pir bg-transparent text-pir-text-tertiary hover:border-pir-strong hover:text-pir-text-primary"
                  }`}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            onClick={() => setTodayOnly((value) => !value)}
            className="mt-3 h-7 rounded-sm border border-pir bg-transparent px-2 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
          >
            {todayOnly ? "oggi" : "tutto"}
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <HistoryContent state={historyState} />
          {historyState.status === "error" && historyState.items.length > 0 && (
            <div className="border-t border-pir px-4 py-2 font-mono text-caption text-pir-warning">
              history stale
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function HistoryContent({
  state,
}: {
  state:
    | { status: "loading"; items: IngestHistoryEntry[] }
    | { status: "ready"; items: IngestHistoryEntry[] }
    | { status: "error"; items: IngestHistoryEntry[] };
}) {
  if (state.status === "loading" && state.items.length === 0) {
    return <HistorySkeleton />;
  }
  if (state.items.length === 0) {
    return <HistoryEmpty error={state.status === "error"} />;
  }
  return (
    <ul className="divide-y divide-pir">
      {state.items.map((entry) => (
        <li key={`${entry.source}:${entry.id}`}>
          <HistoryRow entry={entry} />
        </li>
      ))}
    </ul>
  );
}

function HistoryRow({ entry }: { entry: IngestHistoryEntry }) {
  const target = [entry.target_folder, entry.target_filename].filter(Boolean).join("/");
  return (
    <article className="px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate font-sans text-body font-semibold text-pir-text-primary">
            {entry.filename}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px] text-pir-text-tertiary">
            <span>{entry.project_slug}</span>
            {entry.parser_used && (
              <>
                <span className="text-pir-text-muted">·</span>
                <span>{entry.parser_used}</span>
              </>
            )}
            {entry.file_size_bytes != null && (
              <>
                <span className="text-pir-text-muted">·</span>
                <span>{formatBytes(entry.file_size_bytes)}</span>
              </>
            )}
          </div>
        </div>
        <span
          className={`shrink-0 rounded-sm border px-1.5 py-0.5 font-mono text-[9px] font-semibold tracking-[0.08em] ${DECISION_CLASS[entry.decision]}`}
        >
          {DECISION_LABEL[entry.decision]}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-[88px_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono text-[10px] leading-4">
        {entry.document_type && (
          <>
            <dt className="uppercase tracking-[0.12em] text-pir-text-muted">type</dt>
            <dd className="truncate text-pir-text-secondary">{entry.document_type}</dd>
          </>
        )}
        {entry.confidence != null && (
          <>
            <dt className="uppercase tracking-[0.12em] text-pir-text-muted">conf</dt>
            <dd className="text-pir-text-secondary">{formatConfidence(entry.confidence)}</dd>
          </>
        )}
        {target && (
          <>
            <dt className="uppercase tracking-[0.12em] text-pir-text-muted">target</dt>
            <dd className="truncate text-pir-text-secondary">{target}</dd>
          </>
        )}
        {entry.reason && (
          <>
            <dt className="uppercase tracking-[0.12em] text-pir-text-muted">reason</dt>
            <dd className="line-clamp-2 text-pir-text-secondary">{entry.reason}</dd>
          </>
        )}
        <dt className="uppercase tracking-[0.12em] text-pir-text-muted">time</dt>
        <dd className="text-pir-text-secondary">{formatTimestamp(entry.updated_at)}</dd>
      </dl>
    </article>
  );
}

function HistorySkeleton() {
  return (
    <div className="space-y-3 px-4 py-4">
      {Array.from({ length: 5 }).map((_, index) => (
        <div
          key={index}
          className="h-20 animate-pulse rounded-sm border border-pir bg-pir-surface-1"
        />
      ))}
    </div>
  );
}

function HistoryEmpty({ error }: { error: boolean }) {
  return (
    <div className="flex h-full items-center justify-center px-5 text-center">
      <p className="font-sans text-body text-pir-text-secondary">
        {error ? "History non disponibile." : "Nessuna decisione nel filtro corrente."}
      </p>
    </div>
  );
}

function formatConfidence(value: number): string {
  const normalized = value <= 1 ? value * 100 : value;
  return `${Math.round(normalized)}%`;
}

function formatTimestamp(value: string): string {
  const normalized = value.includes("T") ? value : value.replace(" ", "T") + "Z";
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("it-IT", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
