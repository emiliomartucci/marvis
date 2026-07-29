"use client";

// PipelineSubbar — 6 persistent stations (sub-05 §4.4).
// Reads PipelineCounters in a single call (`/api/v1/brain/counters`).
// Stations cliccabili: Ingest → Inbox/files, Digest → /brain (events),
// Diario/Stride/Memoria/Da decidere → tab dedicate.

import Link from "next/link";
import { usePathname } from "next/navigation";

import type { PipelineCounters } from "@/lib/brain/surfaces";

interface StationDef {
  key: keyof PipelineCounters | "ingest";
  label: string;
  href: string;
}

const STATIONS: StationDef[] = [
  { key: "ingest", label: "INGEST", href: "/inbox/triage/files/" },
  { key: "digest", label: "DIGEST", href: "/brain/" },
  { key: "journal", label: "DIARIO", href: "/brain/diario/" },
  { key: "drift", label: "STRIDE", href: "/brain/stride/" },
  { key: "memory_ops", label: "MEMORIA", href: "/brain/memoria/" },
  { key: "findings", label: "DA DECIDERE", href: "/brain/da-decidere/" },
];

/** @public */
export interface PipelineSubbarProps {
  counters: PipelineCounters | null;
  loading?: boolean;
}

export function PipelineSubbar({ counters, loading = false }: PipelineSubbarProps) {
  const pathname = usePathname();
  return (
    <nav
      aria-label="Pipeline Brain"
      className="sticky top-0 z-10 flex items-center gap-0 border-b border-pir-border bg-[hsl(var(--pir-surface-0))] px-4 py-1.5"
      style={{ borderRadius: "2px" }}
    >
      {STATIONS.map((station, idx) => {
        const isActive =
          pathname === station.href.replace(/\/$/, "") ||
          pathname?.startsWith(station.href);
        const value = counters
          ? Number((counters as unknown as Record<string, number>)[station.key] ?? 0)
          : null;
        return (
          <span key={station.key} className="flex items-center">
            <Link
              href={station.href}
              prefetch={false}
              className={`flex items-baseline gap-2 px-3 py-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] transition-colors ${
                isActive
                  ? "text-pir-text-primary"
                  : "text-pir-text-secondary hover:text-pir-text-primary"
              }`}
              style={{ borderRadius: "2px" }}
            >
              <span>{station.label}</span>
              <span
                aria-label="count"
                className="font-mono text-[11px] text-pir-text-tertiary"
              >
                {loading ? "·" : value ?? "—"}
              </span>
            </Link>
            {idx < STATIONS.length - 1 && (
              <span aria-hidden className="text-pir-text-muted">→</span>
            )}
          </span>
        );
      })}
    </nav>
  );
}
