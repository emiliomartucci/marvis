"use client";

import type { MonitoringSnapshot } from "@/lib/types";

interface Props {
  snapshot: MonitoringSnapshot | null;
}

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h`;
  return `<1h`;
}

const STATUS_DOT: Record<string, string> = {
  running: "bg-green-400",
  stopped: "bg-red-400",
  restarting: "bg-yellow-400",
  created: "bg-gray-400",
  exited: "bg-red-400",
};

export default function DockerSection({ snapshot }: Props) {
  const containers = snapshot?.docker ?? [];

  return (
    <section id="docker">
      <h2 className="text-body font-medium text-pir-text-primary mb-3">
        Docker
      </h2>

      {containers.length === 0 ? (
        <div className="text-caption text-pir-text-muted border border-pir rounded p-4 text-center">
          No containers found
        </div>
      ) : (
        <div className="border border-pir rounded overflow-hidden">
          <table className="w-full text-label">
            <thead>
              <tr className="bg-pir-surface-0 text-caption text-pir-text-muted">
                <th className="text-left px-3 py-2 font-normal">Container</th>
                <th className="text-left px-3 py-2 font-normal">Status</th>
                <th className="text-right px-3 py-2 font-normal">CPU</th>
                <th className="text-right px-3 py-2 font-normal">RAM</th>
                <th className="text-right px-3 py-2 font-normal">Mem%</th>
                <th className="text-right px-3 py-2 font-normal">Restarts</th>
                <th className="text-right px-3 py-2 font-normal">Uptime</th>
              </tr>
            </thead>
            <tbody>
              {containers.map((c) => (
                <tr
                  key={c.name}
                  className="border-t border-pir hover:bg-pir-surface-0/50"
                >
                  <td className="px-3 py-2 font-mono text-pir-text-primary">
                    {c.name}
                  </td>
                  <td className="px-3 py-2">
                    <span className="flex items-center gap-1.5">
                      <span
                        className={`w-1.5 h-1.5 rounded-full ${STATUS_DOT[c.status] ?? "bg-gray-400"}`}
                      />
                      <span className="text-pir-text-secondary">{c.status}</span>
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-pir-text-primary">
                    {c.cpu_pct.toFixed(1)}%
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-pir-text-secondary">
                    {c.memory_mb.toFixed(0)}/{c.memory_limit_mb.toFixed(0)} MB
                  </td>
                  <td className="px-3 py-2 text-right">
                    {c.memory_pct > 0 ? (
                      <div className="flex items-center gap-1.5 justify-end">
                        <div className="w-12 h-1.5 bg-pir-surface-1 rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full ${
                              c.memory_pct >= 80
                                ? "bg-red-500"
                                : c.memory_pct >= 60
                                ? "bg-yellow-400"
                                : "bg-blue-500"
                            }`}
                            style={{ width: `${Math.min(100, c.memory_pct)}%` }}
                          />
                        </div>
                        <span className="font-mono tabular-nums text-pir-text-secondary text-[11px]">
                          {c.memory_pct.toFixed(0)}%
                        </span>
                      </div>
                    ) : (
                      <span className="text-pir-text-muted">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-pir-text-secondary">
                    {c.restart_count}
                  </td>
                  <td className="px-3 py-2 text-right text-pir-text-muted">
                    {c.status === "running"
                      ? formatUptime(c.uptime_seconds)
                      : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
