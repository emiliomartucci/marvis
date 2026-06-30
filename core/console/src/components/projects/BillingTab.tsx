"use client";

import { useEffect, useState } from "react";
import { getProjectBilling } from "@/lib/api";
import type { ProjectBillingSummary } from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

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

function MetricCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: boolean;
}) {
  return (
    <div
      className={`border rounded p-3 ${
        accent
          ? "bg-pir-accent/10 border-pir-accent/30"
          : "bg-pir-surface-1 border-pir"
      }`}
    >
      <div className="text-[11px] uppercase tracking-wider text-pir-text-muted mb-1">{label}</div>
      <div
        className={`text-lg tabular-nums ${
          accent ? "text-pir-accent" : "text-pir-text-primary"
        }`}
      >
        {value}
      </div>
      {sub && <div className="text-[11px] text-pir-text-muted mt-0.5">{sub}</div>}
    </div>
  );
}

function BarRow({
  label,
  value,
  total,
  colorClass,
}: {
  label: string;
  value: number;
  total: number;
  colorClass: string;
}) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-pir-text-secondary">
        <span>{label}</span>
        <span className="tabular-nums">
          ${value.toFixed(4)} <span className="text-pir-text-muted">({pct}%)</span>
        </span>
      </div>
      <div className="h-1.5 bg-pir-surface-2 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${colorClass}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function BillingTab({ slug }: { slug: string }) {
  const [preset, setPreset] = useState<DatePreset>("month");
  const [billing, setBilling] = useState<ProjectBillingSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    const range = getDateRange(preset);
    getProjectBilling(slug, range, { signal: ctrl.signal })
      .then((data) => setBilling(data))
      .catch((err) => {
        if (err.name !== "AbortError") setError(err.message);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [slug, preset]);

  const billVsCost =
    billing && billing.total_cost_usd > 0
      ? ((billing.total_bill_usd / billing.total_cost_usd - 1) * 100).toFixed(0)
      : null;

  return (
    <div className="space-y-5">
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

      {loading ? (
        <div className="text-center py-8 text-sm text-pir-text-muted">Loading...</div>
      ) : error ? (
        <div className="text-center py-8"><ErrorAlert message={error} /></div>
      ) : !billing ? null : (
        <>
          {/* Top summary cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <MetricCard
              label="Total Cost"
              value={`$${billing.total_cost_usd.toFixed(4)}`}
              sub={`${billing.entry_count} entries`}
            />
            <MetricCard
              label="Total Billable"
              value={`$${billing.total_bill_usd.toFixed(4)}`}
              sub={billVsCost !== null ? `${billVsCost}% markup` : undefined}
              accent
            />
            <MetricCard
              label="Tasks"
              value={String(billing.task_count)}
              sub="with cost entries"
            />
            <MetricCard
              label="Period"
              value={billing.from_date}
              sub={`to ${billing.to_date}`}
            />
          </div>

          {/* Breakdown by type */}
          <div className="bg-pir-surface-1 border border-pir rounded p-4 space-y-4">
            <div className="text-[11px] uppercase tracking-wider text-pir-text-muted">
              Cost by type
            </div>
            <BarRow
              label="Agent"
              value={billing.agent_cost_usd}
              total={billing.total_cost_usd}
              colorClass="bg-blue-500"
            />
            <BarRow
              label="Human"
              value={billing.human_cost_usd}
              total={billing.total_cost_usd}
              colorClass="bg-purple-500"
            />
          </div>

          {/* Billable vs non-billable breakdown */}
          <div className="bg-pir-surface-1 border border-pir rounded p-4 space-y-4">
            <div className="text-[11px] uppercase tracking-wider text-pir-text-muted">
              Billable breakdown
            </div>
            <BarRow
              label="Billable"
              value={billing.billable_usd}
              total={billing.total_bill_usd}
              colorClass="bg-green-500"
            />
            <BarRow
              label="Non-billable"
              value={billing.non_billable_usd}
              total={billing.total_cost_usd}
              colorClass="bg-pir-text-muted"
            />
          </div>

          {/* Billing config */}
          <div className="bg-pir-surface-1 border border-pir rounded p-4">
            <div className="text-[11px] uppercase tracking-wider text-pir-text-muted mb-3">
              Billing config
            </div>
            <div className="grid grid-cols-3 gap-4 text-sm">
              <div>
                <div className="text-pir-text-muted text-xs mb-0.5">Token markup</div>
                <div className="text-pir-text-primary tabular-nums">
                  {billing.token_markup_factor.toFixed(2)}x
                </div>
              </div>
              <div>
                <div className="text-pir-text-muted text-xs mb-0.5">Agent rate</div>
                <div className="text-pir-text-primary tabular-nums">
                  ${billing.agent_bill_rate.toFixed(2)}/h
                </div>
              </div>
              <div>
                <div className="text-pir-text-muted text-xs mb-0.5">Human rate</div>
                <div className="text-pir-text-primary tabular-nums">
                  ${billing.human_bill_rate.toFixed(2)}/h
                </div>
              </div>
            </div>
          </div>

          {/* Empty state if no entries */}
          {billing.entry_count === 0 && (
            <div className="text-center py-8">
              <div className="text-pir-text-muted text-sm mb-1">No cost entries in this period</div>
              <div className="text-pir-text-tertiary text-xs">Try selecting a different date range</div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
