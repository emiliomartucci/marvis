// v1.0.0 - 2026-04-24 - Kind/Program label + color mappings (post-mockGraph split, PR #3)
//
// Estratto da `data/mockGraph.ts` (PR #1) al momento della rimozione del mock:
// queste costanti sono logica UI (mapping kind Cosmo -> DocTags, label display)
// e non dati di test, quindi sopravvivono come modulo dedicato.

import { docTagColor, type ActivityKind, type TagColor } from "@/lib/docTags";
import type { Kind, Program } from "./types";

// -----------------------------------------------------------------------------
// Mapping kind Cosmo -> kind attivita' (DocTags)
// -----------------------------------------------------------------------------

/**
 * Bridge fra i kind del design Cosmo (singolari: `plan`, `brainstorm`, ...)
 * e i kind DocTags esistenti (plurali: `plans`, `brainstorms`, ...).
 * Fallback a `docs` per qualsiasi kind non mappabile.
 * @public
 */
export const COSMO_KIND_TO_DOC_KIND: Readonly<Record<Kind, ActivityKind>> = {
  plan: "plans",
  brainstorm: "brainstorms",
  solution: "solutions",
  audit: "audits",
  research: "research",
  handoff: "docs", // nessun slot `handoffs` in DOC_TAG_COLORS
  task: "task",
  learning: "docs", // learning -> fallback docs (nessun slot dedicato)
};

/**
 * Colore canonico per un kind Cosmo, via DOC_TAG_COLORS (source of truth
 * Okabe-Ito). Sostituisce la palette hex-hardcoded del reference HTML.
 * @public
 */
export function kindColor(kind: Kind): TagColor {
  return docTagColor(COSMO_KIND_TO_DOC_KIND[kind]);
}

// -----------------------------------------------------------------------------
// Etichette kind + program (UI chrome)
// -----------------------------------------------------------------------------

/**
 * Label leggibile per kind (usata da HUD legend in PR #2).
 * @public
 */
export const KIND_LABELS: Readonly<Record<Kind, string>> = {
  plan: "plan",
  brainstorm: "brainstorm",
  solution: "solution",
  audit: "audit",
  research: "research",
  handoff: "handoff",
  task: "task",
  learning: "learning",
};

/**
 * Label leggibile per program. Serve solo come display string. Programmi non
 * elencati qui ricadono sul label del program stesso (vedi `programLabel`).
 * @public
 */
export const PROGRAM_LABELS: Readonly<Record<Program, string>> = {
  marvis: "marvis",
  personal: "personal",
};

/**
 * Display label per un program qualsiasi. Usa PROGRAM_LABELS quando disponibile,
 * altrimenti il valore grezzo del program (data-driven, no hardcoded customer names).
 * @public
 */
export function programLabel(program: string): string {
  return (PROGRAM_LABELS as Record<string, string>)[program] ?? program;
}
