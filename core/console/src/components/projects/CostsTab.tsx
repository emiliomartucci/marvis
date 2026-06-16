"use client";

import { useEffect, useState } from "react";
import { getProjectCosts } from "@/lib/api";
import type { ConversationCost } from "@/lib/types";

type DatePreset = "week" | "month" | "last_month" | "all";

const PRESETS: { key: DatePreset; label: string }[] = [
  { key: "week", label: "This Week" },
  { key: "month", label: "This Month" },
  { key: "last_month", label: "Last Month" },
  { key: "all", label: "All Time" },
];

function getDateRange(preset: DatePreset): { from?: string; to?: string } {
  const now = new Date();
  const yyyy = (d: Date) => d.toISOString().slice(0, 10);

  switch (preset) {
    case "week": {
      const start = new Date(now);
      start.setDate(start.getDate() - start.getDay());
      return { from: yyyy(start), to: yyyy(now) };
    }
    case "month": {
      const start = new Date(now.getFullYear(), now.getMonth(), 1);
      return { from: yyyy(start), to: yyyy(now) };
    }
    case "last_month": {
      const start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      const end = new Date(now.getFullYear(), now.getMonth(), 0);
      return { from: yyyy(start), to: yyyy(end) };
    }
    case "all":
      return {};
  }
}

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diffMs = now - then;
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 30) return `${diffDays}d ago`;
  return new Date(dateStr).toLocaleDateString("en", { month: "short", day: "numeric" });
}

function formatDuration(seconds: number): string {
  if (seconds <= 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}k`;
  return String(n);
}

type SortField = "cost_usd" | "message_count" | "updated_at";
type SortDir = "asc" | "desc";

export default function CostsTab({ slug }: { slug: string }) {
  const [preset, setPreset] = useState<DatePreset>("month");
  const [costs, setCosts] = useState<ConversationCost[]>([]);
  const [loading, setLoading] = useState(true);
  const [sortField, setSortField] = useState<SortField>("updated_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [page, setPage] = useState(0);

  const PAGE_SIZE = 25;

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    const range = getDateRange(preset);
    getProjectCosts(slug, { ...range, limit: 200 }, { signal: ctrl.signal })
      .then((data) => {
        setCosts(data);
        setPage(0);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [slug, preset]);

  // Sort
  const sorted = [...costs].sort((a, b) => {
    const mul = sortDir === "asc" ? 1 : -1;
    if (sortField === "cost_usd") return (a.cost_usd - b.cost_usd) * mul;
    if (sortField === "message_count") return ((a.message_count || 0) - (b.message_count || 0)) * mul;
    return ((a.updated_at || "").localeCompare(b.updated_at || "")) * mul;
  });

  const paginated = sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);

  // Summary
  const totalCost = costs.reduce((s, c) => s + c.cost_usd, 0);
  const avgCost = costs.length > 0 ? totalCost / costs.length : 0;
  const totalTokens = costs.reduce((s, c) => s + c.input_tokens + c.output_tokens, 0);
  const completedCount = costs.filter((c) => c.completed_at).length;

  function handleSort(field: SortField) {
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir("desc");
    }
  }

  const sortIcon = (field: SortField) => {
    if (sortField !== field) return null;
    return <span className="ml-0.5">{sortDir === "asc" ? "\u25B4" : "\u25BE"}</span>;
  };

  return (
    <div className="space-y-4">
      {/* Date preset pills */}
      <div className="flex gap-2">
        {PRESETS.map((p) => (
          <button
            key={p.key}
            onClick={() => setPreset(p.key)}
            className={`px-3 py-1.5 text-xs rounded-full transition-colors ${
              preset === p.key
                ? "bg-pir-accent text-white"
                : "bg-pir-surface-1 text-pir-text-secondary hover:bg-pir-surface-2"
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Summary row */}
      <div className="grid grid-cols-4 gap-3">
        <div className="bg-pir-surface-1 border border-pir rounded p-3">
          <div className="text-[11px] uppercase tracking-wider text-pir-text-muted mb-1">Total Cost</div>
          <div className="text-lg text-pir-text-primary tabular-nums">${totalCost.toFixed(2)}</div>
        </div>
        <div className="bg-pir-surface-1 border border-pir rounded p-3">
          <div className="text-[11px] uppercase tracking-wider text-pir-text-muted mb-1">Sessions</div>
          <div className="text-lg text-pir-text-primary tabular-nums">
            {costs.length}
            {completedCount > 0 && (
              <span className="text-xs text-pir-text-muted ml-1">({completedCount} done)</span>
            )}
          </div>
        </div>
        <div className="bg-pir-surface-1 border border-pir rounded p-3">
          <div className="text-[11px] uppercase tracking-wider text-pir-text-muted mb-1">Total Tokens</div>
          <div className="text-lg text-pir-text-primary tabular-nums">{formatTokens(totalTokens)}</div>
        </div>
        <div className="bg-pir-surface-1 border border-pir rounded p-3">
          <div className="text-[11px] uppercase tracking-wider text-pir-text-muted mb-1">Avg / Session</div>
          <div className="text-lg text-pir-text-primary tabular-nums">${avgCost.toFixed(2)}</div>
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-center py-8 text-sm text-pir-text-muted">Loading...</div>
      ) : costs.length === 0 ? (
        <div className="text-center py-12">
          <div className="text-pir-text-muted text-sm mb-1">No conversations in this period</div>
          <div className="text-pir-text-tertiary text-xs">Try selecting a different date range</div>
        </div>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-pir text-pir-text-muted text-[11px] uppercase tracking-wider">
                  <th className="text-left py-2 px-2 sticky left-0 bg-pir-base">Session</th>
                  <th className="text-left py-2 px-2">Model</th>
                  <th
                    className="text-right py-2 px-2 cursor-pointer hover:text-pir-text-primary"
                    onClick={() => handleSort("cost_usd")}
                  >
                    Cost {sortIcon("cost_usd")}
                  </th>
                  <th className="text-right py-2 px-2">Tokens</th>
                  <th
                    className="text-right py-2 px-2 cursor-pointer hover:text-pir-text-primary"
                    onClick={() => handleSort("message_count")}
                  >
                    Msgs {sortIcon("message_count")}
                  </th>
                  <th className="text-right py-2 px-2">Active</th>
                  <th className="text-center py-2 px-2">Status</th>
                  <th
                    className="text-right py-2 px-2 cursor-pointer hover:text-pir-text-primary"
                    onClick={() => handleSort("updated_at")}
                  >
                    Date {sortIcon("updated_at")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {paginated.map((c) => (
                  <tr key={c.conversation_id} className="border-b border-pir/50 hover:bg-pir-surface-1/50">
                    <td className="py-2 px-2 sticky left-0 bg-pir-base">
                      <div className="text-pir-text-primary font-mono truncate max-w-[180px]">
                        {c.session_name || c.conversation_id.slice(0, 8)}
                      </div>
                      {c.display_name && (
                        <div className="text-[11px] text-pir-text-muted truncate max-w-[180px]">
                          {c.display_name}
                        </div>
                      )}
                    </td>
                    <td className="py-2 px-2 text-pir-text-secondary text-xs">
                      {c.model ? c.model.replace("claude-", "").replace("-4-6", "") : "—"}
                    </td>
                    <td className="py-2 px-2 text-right text-pir-text-primary tabular-nums">
                      ${c.cost_usd.toFixed(2)}
                    </td>
                    <td className="py-2 px-2 text-right text-pir-text-muted tabular-nums text-xs">
                      {c.input_tokens + c.output_tokens > 0 ? (
                        <span title={`In: ${formatTokens(c.input_tokens)} / Out: ${formatTokens(c.output_tokens)}`}>
                          {formatTokens(c.input_tokens + c.output_tokens)}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="py-2 px-2 text-right text-pir-text-secondary tabular-nums">
                      {c.message_count}
                    </td>
                    <td className="py-2 px-2 text-right text-pir-text-muted tabular-nums text-xs">
                      {formatDuration(c.working_seconds)}
                    </td>
                    <td className="py-2 px-2 text-center">
                      {c.completed_at ? (
                        <span className="inline-block px-1.5 py-0.5 text-[10px] rounded bg-green-900/30 text-green-400 border border-green-800/30">
                          done
                        </span>
                      ) : (
                        <span className="inline-block px-1.5 py-0.5 text-[10px] rounded bg-blue-900/30 text-blue-400 border border-blue-800/30">
                          active
                        </span>
                      )}
                    </td>
                    <td className="py-2 px-2 text-right text-pir-text-muted" title={c.updated_at || ""}>
                      {c.updated_at ? timeAgo(c.updated_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between text-xs text-pir-text-muted">
              <span>
                Page {page + 1} of {totalPages}
              </span>
              <div className="flex gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0}
                  className="px-2 py-1 rounded bg-pir-surface-1 hover:bg-pir-surface-2 disabled:opacity-30"
                >
                  Prev
                </button>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                  disabled={page >= totalPages - 1}
                  className="px-2 py-1 rounded bg-pir-surface-1 hover:bg-pir-surface-2 disabled:opacity-30"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
