"use client";

import { useState } from "react";
import type { MonitoringSnapshot } from "@/lib/types";
import MetricCard from "../MetricCard";
import CandleChart from "../charts/CandleChart";

interface Props {
  snapshot: MonitoringSnapshot | null;
}

function formatBandwidth(bps: number): { value: string; unit: string } {
  if (bps >= 1_000_000) return { value: (bps / 1_000_000).toFixed(1), unit: "MB/s" };
  if (bps >= 1_000) return { value: (bps / 1_000).toFixed(1), unit: "KB/s" };
  return { value: bps.toFixed(0), unit: "B/s" };
}

const CONNECTIVITY_DOT: Record<string, string> = {
  connected: "bg-green-400",
  active: "bg-green-400",
  disconnected: "bg-red-400",
  inactive: "bg-red-400",
  unknown: "bg-gray-400",
};

const NET_METRICS = ["net_rx_bps", "net_tx_bps"] as const;

export default function NetworkSection({ snapshot }: Props) {
  const [expandedMetric, setExpandedMetric] = useState<string | null>(null);
  const system = snapshot?.system;
  const network = snapshot?.network;
  const sparklines = snapshot?.sparklines ?? {};

  const rx = system ? formatBandwidth(system.net_rx_bps) : null;
  const tx = system ? formatBandwidth(system.net_tx_bps) : null;

  return (
    <section id="network">
      <h2 className="text-body font-medium text-pir-text-primary mb-3">
        Network
      </h2>

      {!system ? (
        <div className="text-caption text-pir-text-muted border border-pir rounded p-4 text-center">
          Collecting data...
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <MetricCard
              label="Download"
              value={rx?.value ?? "0"}
              unit={rx?.unit}
              sparkline={sparklines.net_rx_bps}
            />
            <MetricCard
              label="Upload"
              value={tx?.value ?? "0"}
              unit={tx?.unit}
              sparkline={sparklines.net_tx_bps}
            />
          </div>

          <div className="flex gap-2">
            {NET_METRICS.map((m) => (
              <button
                key={m}
                onClick={() => setExpandedMetric(expandedMetric === m ? null : m)}
                className={`text-caption px-2 py-0.5 rounded border transition-colors ${
                  expandedMetric === m
                    ? "border-pir-accent text-pir-accent"
                    : "border-pir text-pir-text-muted hover:text-pir-text-secondary"
                }`}
              >
                {m === "net_rx_bps" ? "rx chart" : "tx chart"}
              </button>
            ))}
          </div>

          {expandedMetric && (
            <div className="border border-pir rounded p-3 bg-pir-surface-0">
              <CandleChart metric={expandedMetric} />
            </div>
          )}

          {network && (
            <div className="border border-pir rounded p-3 bg-pir-surface-0">
              <div className="text-caption text-pir-text-muted mb-2">
                Connectivity
              </div>
              <div className="flex flex-wrap gap-4 text-label">
                <span className="flex items-center gap-1.5">
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${CONNECTIVITY_DOT[network.tailscale] ?? "bg-gray-400"}`}
                  />
                  <span className="text-pir-text-secondary">Tailscale</span>
                  {network.tailscale_ip && (
                    <span className="font-mono text-pir-text-muted">
                      {network.tailscale_ip}
                    </span>
                  )}
                </span>
                <span className="flex items-center gap-1.5">
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${CONNECTIVITY_DOT[network.cf_tunnel] ?? "bg-gray-400"}`}
                  />
                  <span className="text-pir-text-secondary">CF Tunnel</span>
                </span>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
