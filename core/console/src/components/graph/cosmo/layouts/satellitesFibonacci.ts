// v2.2.0 - 2026-04-24 - PlacedSatellite.items: propagazione SatelliteItem[] da BE
// v2.1.0 - 2026-04-24 - SatelliteSummary input: count + latest_at su PlacedSatellite
// v2.0.0 - 2026-04-24 - Rimossa dipendenza DIR_PATHS_BY_PROGRAM (mock paths)
// v1.0.0 - 2026-04-24 - Satellites Fibonacci packing (PR #1 foundation).
//
// Ritorna le posizioni assolute dei satelliti (kind chip) intorno a un progetto
// posizionato. Usa FIB_PACKING pre-computato (coordinate normalizzate a parent
// radius = 1) e moltiplica per `project.r * 0.92` (margine 8%).
//
// Post-PR3 (live data): il satellite rappresenta un KIND (plan/handoff/audit),
// NON una directory fisica. Il `path` mock derivato da DIR_PATHS_BY_PROGRAM
// era un artefatto del reference HTML con dati fake. Rimosso.
//
// v2.1.0: input passa da `Kind[]` a `SatelliteSummary[]` per propagare
// `count` (numero artifact totali del kind, per file-dot N=min(count,6)) +
// `latest_at` (per accent burning solo se < 7 giorni).
//
// Porta di reference-graph-v1-cosmo.html righe 523-557.

import type { Kind, PlacedNode, SatelliteItem, SatelliteSummary } from "../types";
import { FIB_PACKING } from "./forceLayoutHelpers";

/** Nome leggibile del kind (uppercase breve), usato nel breadcrumb HUD. */
const KIND_LABELS: Readonly<Record<Kind, string>> = {
  plan: "Plans",
  brainstorm: "Brainstorms",
  solution: "Solutions",
  audit: "Audits",
  research: "Research",
  handoff: "Handoffs",
  task: "Tasks",
  learning: "Learnings",
};

/**
 * Satellite posizionato intorno al progetto (coords assolute viewport).
 * @lintignore — consumato da GraphCanvas satellite rendering PR #2.
 */
export interface PlacedSatellite {
  kind: Kind;
  x: number;
  y: number;
  r: number;
  /** Indice in ordine di recency (0 = piu' recente). */
  recencyIdx: number;
  /** Nome leggibile del kind (es. "Plans", "Handoffs") per breadcrumb/inspector. */
  name: string;
  /** Numero Fibonacci del slot (1/2/3/5/8/…), utile per label intra-disk. */
  fibValue: number;
  /** Numero artifact totali del kind nel project (no cap top-8). */
  count: number;
  /** ISO timestamp dell'artifact piu' recente del kind (null se 0). */
  latest_at: string | null;
  /** Top-12 SatelliteItem cliccabili dal BE (Q2 v1.2.0). Vuota se BE pre-v1.2.0. */
  items: readonly SatelliteItem[];
}

/**
 * Layout fibonacci-packed dei satelliti attorno a `project`.
 * @param project nodo progetto gia' posizionato
 * @param summaries lista SatelliteSummary (ordinata per recency desc: primo = piu' recente)
 * @returns satelliti con coordinate assolute. Cap a 20 (FIB_PACKING.length).
 * @public
 */
export function layoutSatellitesFib(
  project: PlacedNode,
  summaries: readonly SatelliteSummary[],
): PlacedSatellite[] {
  if (summaries.length === 0) return [];
  const cap = Math.min(FIB_PACKING.length, summaries.length);
  const slots = FIB_PACKING.slice(0, cap);

  return slots.map((s, i) => {
    const summary = summaries[i];
    return {
      kind: summary.kind,
      recencyIdx: i,
      name: KIND_LABELS[summary.kind],
      fibValue: s.fib,
      count: summary.count,
      latest_at: summary.latest_at ?? null,
      items: summary.items ?? [],
      r: s.r * project.r * 0.92,
      x: project.x + s.x * project.r * 0.92,
      y: project.y + s.y * project.r * 0.92,
    };
  });
}
