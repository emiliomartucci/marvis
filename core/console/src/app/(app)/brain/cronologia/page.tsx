"use client";

import { useEffect, useState } from "react";

import { useBrainContext } from "@/components/brain/useBrainContext";
import { PanelEmpty, PanelLoading } from "@/components/brain/Panels";
import { fetchRuns } from "@/lib/brain/surfaces";
import type { BrainRun } from "@/lib/brain/types";

function filterButtonClass(active: boolean): string {
  return active
    ? "border-[hsl(var(--pir-accent))] bg-[hsl(var(--pir-accent)/0.12)] text-pir-text-primary"
    : "border-pir-border text-pir-text-secondary hover:text-pir-text-primary";
}

const STATUS_COLOR: Record<string, string> = {
  succeeded: "text-[hsl(var(--pir-success))]",
  partial: "text-[hsl(var(--pir-warning))]",
  failed: "text-[hsl(var(--pir-error))]",
  running: "text-[hsl(var(--pir-accent))]",
  superseded: "text-pir-text-tertiary",
};

export default function BrainCronologiaPage() {
  const ctx = useBrainContext();
  const [runs, setRuns] = useState<BrainRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string | "all">("all");

  useEffect(() => {
    let active = true;
    setLoading(true);
    async function load() {
      try {
        const resp = await fetchRuns({
          status: statusFilter === "all" ? undefined : [statusFilter],
          include_superseded: true,
          limit: 100,
        });
        if (!active) return;
        setRuns(resp.items as BrainRun[]);
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [statusFilter, ctx.lastWsEvent]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]">
        {(["all", "succeeded", "partial", "failed", "running", "superseded"] as const).map((opt) => (
          <button
            key={opt}
            type="button"
            onClick={() => setStatusFilter(opt)}
            className={`border px-3 py-1 ${filterButtonClass(statusFilter === opt)}`}
            style={{ borderRadius: "2px" }}
          >
            {opt}
          </button>
        ))}
      </div>

      {loading && <PanelLoading message="cronologia · raccogliendo cicli" />}
      {!loading && runs.length === 0 && <PanelEmpty message="Nessun ciclo recente" />}
      {!loading && runs.length > 0 && (
        <table
          className="w-full border-collapse border border-pir-border text-left font-[var(--font-exo-2)] text-sm"
          style={{ borderRadius: "2px" }}
        >
          <thead className="bg-[hsl(var(--pir-surface-2))] text-pir-text-tertiary">
            <tr>
              <th className="border-b border-pir-border px-3 py-2 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]">
                cycle_key
              </th>
              <th className="border-b border-pir-border px-3 py-2 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]">
                status
              </th>
              <th className="border-b border-pir-border px-3 py-2 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]">
                trigger
              </th>
              <th className="border-b border-pir-border px-3 py-2 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]">
                events
              </th>
              <th className="border-b border-pir-border px-3 py-2 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]">
                duration
              </th>
              <th className="border-b border-pir-border px-3 py-2 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]">
                started_at
              </th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const run = r as {
                run_id?: string;
                cycle_key?: string;
                status?: string;
                trigger?: string;
                event_count?: number;
                duration_ms?: number | null;
                started_at?: string;
              };
              return (
                <tr
                  key={run.run_id ?? `run-${runs.indexOf(r)}`}
                  className="hover:bg-[hsl(var(--pir-surface-2))]"
                >
                  <td className="border-b border-pir-border px-3 py-1.5 font-[var(--font-jetbrains-mono)] text-pir-text-primary">
                    {run.cycle_key ?? "—"}
                  </td>
                  <td
                    className={`border-b border-pir-border px-3 py-1.5 ${
                      STATUS_COLOR[run.status ?? ""] ?? "text-pir-text-primary"
                    }`}
                  >
                    {run.status ?? "—"}
                  </td>
                  <td className="border-b border-pir-border px-3 py-1.5 text-pir-text-secondary">
                    {run.trigger ?? "—"}
                  </td>
                  <td className="border-b border-pir-border px-3 py-1.5 text-pir-text-secondary">
                    {run.event_count ?? 0}
                  </td>
                  <td className="border-b border-pir-border px-3 py-1.5 text-pir-text-secondary">
                    {run.duration_ms != null
                      ? `${(run.duration_ms / 1000).toFixed(1)}s`
                      : "—"}
                  </td>
                  <td className="border-b border-pir-border px-3 py-1.5 font-[var(--font-jetbrains-mono)] text-[11px] text-pir-text-tertiary">
                    {run.started_at ?? "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
