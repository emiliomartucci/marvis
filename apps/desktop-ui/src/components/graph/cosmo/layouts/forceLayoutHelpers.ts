// v1.0.0 - 2026-04-24 - Helper condivisi fra layouts Cosmo (PR #1 foundation).
//
// Tutto deterministic + framework-free. Nessun import React.
// Fonte letterale: reference-graph-v1-cosmo.html (2026-04-24) righe 60-67,
// 131, 380-465, 467-521.

import type { Kind, Project } from "../types";

// -----------------------------------------------------------------------------
// PRNG deterministico
// -----------------------------------------------------------------------------

/**
 * Canonical Tommy Ettinger PRNG (mulberry32). `Math.imul` obbligatorio per
 * mantenere la semantica a 32 bit cross-engine (V8/JSC/SpiderMonkey).
 * Stesso seed = stesso stream, test determinismo dipende da questo.
 * @public
 */
export function mulberry32(seed: number): () => number {
  let a = seed;
  return function (): number {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// -----------------------------------------------------------------------------
// Costanti layout
// -----------------------------------------------------------------------------

/**
 * Raggio visuale del super-nodo progetto — legacy degree-based.
 * Mantenuta per compat (test fixture + eventuali consumer single-shot).
 * I layout production usano `computeProjectRadii(projects)` rank-based.
 * @public
 */
export function projectRadius(degree: number): number {
  return Math.max(20, Math.min(220, Math.pow(degree, 0.62) * 4.2));
}

/**
 * Power-law radii (2026-05-17, PR5 unify con Codex). Era rank-based 1.35
 * (rank 8+ saturava a clamp 22 → ~70 progetti tutti 22px, contrasto eccessivo
 * fra top-3 e resto). Adesso formula identica a Codex moduleRadius con range
 * Cosmo: `pow(degree, 0.55) * 7` clamp [36, 220]. Risultato: tutti i progetti
 * visibili (min 36px), top hub scalano fino a 220 in maniera continua.
 *
 * Ordinamento mantenuto via degree (Map ritornata, l'ordine seed e' del caller).
 * @public
 */
export function computeProjectRadii(
  projects: readonly Project[],
): Map<string, number> {
  const MAX_R = 220;
  const MIN_R = 36;
  const radii = new Map<string, number>();
  for (const p of projects) {
    const effective = p.degree > 0 ? p.degree : 1;
    const r = Math.max(MIN_R, Math.min(MAX_R, Math.pow(effective, 0.55) * 7));
    radii.set(p.slug, r);
  }
  return radii;
}

/**
 * Gap minimo fra cerchi nel layout. Abbassato a 4 (grappolo d'uva, 2026-05-16):
 * acini quasi tangenti. Resta condiviso fra initial layout + drag repulsion
 * per evitare oscillazioni ai bordi fra steady-state e mano utente.
 * @public
 */
export const MIN_GAP = 4;

/**
 * Viewport di riferimento per i layout deterministici (constellation / galaxy
 * / force-fresh). Calibrato per 81 progetti con breathing room. In PR #2 il
 * canvas puo' scalare diversamente usando pan/zoom, ma le coordinate LS
 * restano ancorate a questo sistema.
 * @public
 */
export const LAYOUT_VIEWPORT = { w: 2400, h: 1500 } as const;

/**
 * Whitelist condivisa di kind satellite. Usata sia dal mock (validation
 * schema) sia — in PR #3 — dal server-side filter (H-11 piano). Tenerla qui
 * come const uniforme evita drift.
 * @public
 */
export const ALLOWED_KINDS: readonly Kind[] = [
  "plan",
  "brainstorm",
  "solution",
  "audit",
  "research",
  "handoff",
  "task",
  "learning",
] as const;

// -----------------------------------------------------------------------------
// Fibonacci packing (satelliti intorno al nodo progetto)
// -----------------------------------------------------------------------------

// Reference HTML righe 380-394: tier decrescenti per potenza di 1/phi,
// quantita' per tier = sequenza Fibonacci (1, 1, 2, 3, 5, 8 = 20 totali).
const PHI = 1.6180339887;
interface FibTier {
  readonly count: number;
  readonly ratio: number;
}
const TIERS: readonly FibTier[] = [
  { count: 1, ratio: 1 / PHI },
  { count: 1, ratio: 1 / (PHI * PHI) },
  { count: 2, ratio: 1 / Math.pow(PHI, 3) },
  { count: 3, ratio: 1 / Math.pow(PHI, 4) },
  { count: 5, ratio: 1 / Math.pow(PHI, 5) },
  { count: 8, ratio: 1 / Math.pow(PHI, 6) },
];

/**
 * Cerchio packato — coordinate normalizzate a raggio genitore = 1.
 * @lintignore — consumato da GraphCanvas satellite rendering PR #2.
 */
export interface FibCircle {
  readonly r: number;
  readonly x: number;
  readonly y: number;
  readonly tier: number;
  readonly fib: number;
}

const PARENT_R = 1;
const PACK_MARGIN = 0.97;
const OVERLAP_EPSILON = 0.002;

interface PackItem {
  readonly r: number;
  readonly tier: number;
}

function buildQueue(): PackItem[] {
  const queue: PackItem[] = [];
  TIERS.forEach((t, tierIdx) => {
    for (let k = 0; k < t.count; k++) {
      queue.push({ r: t.ratio * PARENT_R, tier: tierIdx });
    }
  });
  return queue;
}

function buildCircle(
  r: number,
  x: number,
  y: number,
  tier: number,
  unit: number,
): FibCircle {
  return { r, x, y, tier, fib: Math.max(1, Math.round(r / unit)) };
}

function isInsideParent(x: number, y: number, r: number): boolean {
  return Math.hypot(x, y) + r <= PARENT_R * PACK_MARGIN;
}

function overlapsAny(
  x: number,
  y: number,
  r: number,
  circles: readonly FibCircle[],
  skip: FibCircle | null,
): boolean {
  for (const o of circles) {
    if (o === skip) continue;
    if (Math.hypot(x - o.x, y - o.y) < o.r + r - OVERLAP_EPSILON) return true;
  }
  return false;
}

/** Cerca la posizione tangente piu' vicina al centro genitore. */
function findTangentSlot(
  r: number,
  tier: number,
  circles: readonly FibCircle[],
  unit: number,
): FibCircle | null {
  let best: FibCircle | null = null;
  let bestScore = Infinity;
  for (const c of circles) {
    const d = c.r + r;
    for (let step = 0; step < 360; step += 4) {
      const ang = (step * Math.PI) / 180;
      const cx = c.x + Math.cos(ang) * d;
      const cy = c.y + Math.sin(ang) * d;
      if (!isInsideParent(cx, cy, r)) continue;
      if (overlapsAny(cx, cy, r, circles, c)) continue;
      const score = Math.hypot(cx, cy);
      if (score < bestScore) {
        bestScore = score;
        best = buildCircle(r, cx, cy, tier, unit);
      }
    }
  }
  return best;
}

/** Fallback grid-scan per microcerchi quando la ricerca tangente fallisce. */
function findGridSlot(
  r: number,
  tier: number,
  circles: readonly FibCircle[],
  unit: number,
): FibCircle | null {
  const step = r * 0.6;
  for (let cx = -PARENT_R + r; cx <= PARENT_R - r; cx += step) {
    for (let cy = -PARENT_R + r; cy <= PARENT_R - r; cy += step) {
      if (!isInsideParent(cx, cy, r)) continue;
      if (overlapsAny(cx, cy, r, circles, null)) continue;
      return buildCircle(r, cx, cy, tier, unit);
    }
  }
  return null;
}

/**
 * Packing Fibonacci deterministic intorno a un cerchio genitore di raggio 1.
 * Strategia: seed il primo a destra sul bordo, poi per ogni successivo prova
 * 90 posizioni tangenti (step 4deg) attorno ad ogni gia' posizionato e sceglie
 * la piu' vicina al centro (packing compatto). Fallback grid-scan per gli
 * ultimi microcerchi. Copia letterale da reference righe 396-463.
 */
function packFibonacci(): readonly FibCircle[] {
  const queue = buildQueue();
  const first = queue.shift();
  if (!first) return [];
  const unit = TIERS[TIERS.length - 1].ratio;
  const circles: FibCircle[] = [
    buildCircle(first.r, PARENT_R * PACK_MARGIN - first.r, 0, first.tier, unit),
  ];

  for (const item of queue) {
    const slot =
      findTangentSlot(item.r, item.tier, circles, unit) ??
      findGridSlot(item.r, item.tier, circles, unit);
    if (slot) circles.push(slot);
  }

  return circles;
}

/**
 * Packing Fibonacci pre-computato (20 cerchi). Coordinate normalizzate a
 * parent radius = 1. Moltiplicare per `projectR * 0.92` per ottenere le
 * coordinate finali dei satelliti (vedi `layoutSatellitesFib`).
 * @public
 */
export const FIB_PACKING: readonly FibCircle[] = packFibonacci();
