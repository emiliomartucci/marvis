// v1.0.0 - 2026-04-22 - §7 Costs summary 4 tiles (PR #9)
"use client";

import { useEffect, useState } from "react";
import SectionShell from "./SectionShell";
import { getProjectCosts } from "@/lib/api";
import type { ConversationCost } from "@/lib/types";

function fmtDollar(v: number): string {
  if (!Number.isFinite(v)) return "—";
  if (v < 10) return `$${v.toFixed(2)}`;
  return `$${v.toFixed(0)}`;
}

function fmtTokens(v: number): string {
  if (!Number.isFinite(v) || v === 0) return "—";
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

function Tile({
  label,
  value,
  subtitle,
  accent,
}: {
  label: string;
  value: string;
  subtitle?: string;
  accent?: boolean;
}) {
  return (
    <div
      className="bg-pir-surface-0 border border-pir"
      style={{ borderRadius: 4, padding: "12px 14px" }}
    >
      <div
        className="text-pir-text-tertiary uppercase"
        style={{
          fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
          fontSize: 9,
          fontWeight: 600,
          letterSpacing: "0.22em",
        }}
      >
        {label}
      </div>
      <div
        className={accent ? "text-pir-accent" : "text-pir-text-primary"}
        style={{
          fontFamily: "var(--pir-font-sans, system-ui)",
          fontSize: 18,
          fontWeight: 700,
          letterSpacing: "-0.01em",
          marginTop: 6,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
      </div>
      {subtitle && (
        <div
          className="text-pir-text-muted"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 10,
            marginTop: 3,
            lineHeight: 1.3,
          }}
        >
          {subtitle}
        </div>
      )}
    </div>
  );
}

function CostsSummarySection({
  slug,
  onOpenBreakdown,
}: {
  slug: string;
  onOpenBreakdown: () => void;
}) {
  const [costs, setCosts] = useState<ConversationCost[] | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const ctrl = new AbortController();
    getProjectCosts(slug, {}, { signal: ctrl.signal })
      .then((res) => setCosts(res))
      .catch(() => setCosts([]))
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [slug]);

  const now = Date.now();
  const WEEK = 7 * 24 * 3600 * 1000;
  const MONTH = 30 * 24 * 3600 * 1000;

  let cost7 = 0;
  let cost30 = 0;
  let tokensIn30 = 0;
  let tokensOut30 = 0;
  let count7 = 0;
  let count30 = 0;

  for (const c of costs || []) {
    const ts = new Date(c.created_at || c.completed_at || c.updated_at || "").getTime();
    if (!Number.isFinite(ts)) continue;
    const age = now - ts;
    if (age <= MONTH) {
      cost30 += c.cost_usd;
      tokensIn30 += c.input_tokens || 0;
      tokensOut30 += c.output_tokens || 0;
      count30 += 1;
      if (age <= WEEK) {
        cost7 += c.cost_usd;
        count7 += 1;
      }
    }
  }

  return (
    <SectionShell
      anchorId="costs"
      eyebrow="Costs · billing"
      title={`Economy · ${slug}`}
      action={
        <button
          type="button"
          onClick={onOpenBreakdown}
          className="text-pir-text-tertiary hover:text-pir-accent transition-colors bg-transparent border border-pir cursor-pointer"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            padding: "4px 8px",
            borderRadius: 2,
          }}
        >
          ↗ Breakdown by task
        </button>
      }
    >
      {loading ? (
        <div className="text-pir-text-tertiary text-sm px-2 py-4">Loading costs…</div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
          <Tile label="7d" value={fmtDollar(cost7)} subtitle={count7 > 0 ? `${count7} sessions` : "—"} />
          <Tile
            label="30d"
            value={fmtDollar(cost30)}
            subtitle={count30 > 0 ? `avg ${fmtDollar(cost30 / Math.max(count30, 1))}/session` : "—"}
          />
          <Tile label="Tokens in" value={fmtTokens(tokensIn30)} subtitle="30d" />
          <Tile label="Tokens out" value={fmtTokens(tokensOut30)} subtitle="30d" accent />
        </div>
      )}
    </SectionShell>
  );
}

export default CostsSummarySection;
