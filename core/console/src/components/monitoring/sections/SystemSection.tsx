"use client";

import { useState } from "react";
import type { MonitoringSnapshot } from "@/lib/types";
import MetricCard from "../MetricCard";
import CandleChart from "../charts/CandleChart";
import DiskTreemap from "../DiskTreemap";

interface Props {
  snapshot: MonitoringSnapshot | null;
}

// disk_pct excluded — disk changes too slowly for time-series; treemap is better
const CHART_CONFIGS = [
  { metric: "cpu_pct", label: "cpu chart", chartType: "candle" as const },
  { metric: "ram_pct", label: "ram chart", chartType: "line" as const },
] as const;

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

function formatBytes(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(0)} GB`;
  return `${mb} MB`;
}

export default function SystemSection({ snapshot }: Props) {
  const [expandedMetric, setExpandedMetric] = useState<string | null>(null);
  const [showDiskMap, setShowDiskMap] = useState(false);
  const system = snapshot?.system;
  const sparklines = snapshot?.sparklines ?? {};
  const alerts = snapshot?.alerts ?? [];

  const cpuAlert = alerts.some((a) => a.metric === "cpu_pct");
  const ramAlert = alerts.some((a) => a.metric === "ram_pct");
  const diskAlert = alerts.some((a) => a.metric === "disk_pct");

  const expandedConfig = CHART_CONFIGS.find((c) => c.metric === expandedMetric);

  return (
    <section id="system">
      <h2 className="text-body font-medium text-pir-text-primary mb-3">
        System
      </h2>

      {!system ? (
        <div className="text-caption text-pir-text-muted border border-pir rounded p-4 text-center">
          Collecting data...
        </div>
      ) : (
        <>
          {alerts.length > 0 && (
            <div className="mb-3 px-3 py-2 rounded border border-yellow-500/40 bg-yellow-500/5 text-caption text-yellow-400">
              {alerts.map((a) => (
                <span key={a.metric} className="mr-3">
                  {a.metric.replace("_pct", "").toUpperCase()} at{" "}
                  {a.value.toFixed(1)}% (threshold: {a.threshold}%)
                </span>
              ))}
            </div>
          )}

          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <MetricCard
              label="CPU"
              value={system.cpu_pct.toFixed(1)}
              unit="%"
              sparkline={sparklines.cpu_pct}
              alert={cpuAlert}
            />
            <MetricCard
              label="RAM"
              value={system.ram_pct.toFixed(1)}
              unit={`% (${system.ram_used_mb.toFixed(0)}/${system.ram_total_mb.toFixed(0)} MB)`}
              sparkline={sparklines.ram_pct}
              alert={ramAlert}
            />
            <MetricCard
              label="Disk"
              value={system.disk_pct.toFixed(1)}
              unit={`% (${system.disk_used_gb}/${system.disk_total_gb} GB)`}
              sparkline={sparklines.disk_pct}
              alert={diskAlert}
            />
            <MetricCard
              label="Load"
              value={system.load_1m.toFixed(2)}
              unit={`/ ${system.load_5m.toFixed(2)} / ${system.load_15m.toFixed(2)}`}
            />
          </div>

          {/* Machine specs row */}
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-caption text-pir-text-muted">
            {system.cpu_count > 0 && (
              <span>
                <span className="text-pir-text-secondary">{system.cpu_count}</span> vCPU
              </span>
            )}
            {system.ram_total_mb > 0 && (
              <span>
                <span className="text-pir-text-secondary">{formatBytes(system.ram_total_mb)}</span> RAM
              </span>
            )}
            {system.disk_total_gb > 0 && (
              <span>
                <span className="text-pir-text-secondary">{system.disk_total_gb} GB</span> disk
              </span>
            )}
            <span>
              up <span className="text-pir-text-secondary">{formatUptime(system.uptime_seconds)}</span>
            </span>
          </div>

          <div className="mt-3 flex gap-2 flex-wrap">
            {CHART_CONFIGS.map((cfg) => (
              <button
                key={cfg.metric}
                onClick={() =>
                  setExpandedMetric(expandedMetric === cfg.metric ? null : cfg.metric)
                }
                className={`text-caption px-2 py-0.5 rounded border transition-colors ${
                  expandedMetric === cfg.metric
                    ? "border-pir-accent text-pir-accent"
                    : "border-pir text-pir-text-muted hover:text-pir-text-secondary"
                }`}
              >
                {cfg.label}
              </button>
            ))}
            <button
              onClick={() => setShowDiskMap(!showDiskMap)}
              className={`text-caption px-2 py-0.5 rounded border transition-colors ${
                showDiskMap
                  ? "border-pir-accent text-pir-accent"
                  : "border-pir text-pir-text-muted hover:text-pir-text-secondary"
              }`}
            >
              disk map
            </button>
          </div>

          {expandedMetric && expandedConfig && (
            <div className="mt-3 border border-pir rounded p-3 bg-pir-surface-0">
              <CandleChart
                metric={expandedMetric}
                chartType={expandedConfig.chartType}
                fixedYRange={[0, 100]}
              />
            </div>
          )}

          {showDiskMap && (
            <div className="mt-3 border border-pir rounded p-3 bg-pir-surface-0">
              <DiskTreemap onClose={() => setShowDiskMap(false)} />
            </div>
          )}
        </>
      )}
    </section>
  );
}
