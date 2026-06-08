"use client";

import type { MonitoringSnapshot } from "@/lib/types";

interface Props {
  snapshot: MonitoringSnapshot | null;
}

const STATUS_DOT: Record<string, string> = {
  running: "bg-green-400",
  stopped: "bg-red-400",
  failed: "bg-red-400",
  unknown: "bg-gray-400",
};

export default function ServicesSection({ snapshot }: Props) {
  const services = snapshot?.services ?? [];

  return (
    <section id="services">
      <h2 className="text-body font-medium text-pir-text-primary mb-3">
        Services
      </h2>

      {services.length === 0 ? (
        <div className="text-caption text-pir-text-muted border border-pir rounded p-4 text-center">
          Collecting data...
        </div>
      ) : (
        <div className="border border-pir rounded divide-y divide-pir">
          {services.map((s) => (
            <div
              key={s.name}
              className="flex items-center gap-3 px-3 py-2 bg-pir-surface-0"
            >
              <span
                className={`w-1.5 h-1.5 rounded-full shrink-0 ${STATUS_DOT[s.status] ?? "bg-gray-400"}`}
              />
              <span className="text-label text-pir-text-primary font-medium">
                {s.name}
              </span>
              <span className="text-caption text-pir-text-muted">{s.status}</span>
              {s.details && (
                <span className="text-caption text-pir-text-muted ml-auto font-mono truncate max-w-[200px]">
                  {s.details}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
