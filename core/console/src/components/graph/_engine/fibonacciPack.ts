// v1.0.0 - 2026-05-17 - Engine condiviso Cosmo+Codex: Fibonacci packing generic.
//
// Pack N cerchi in tier potenze decrescenti di 1/phi all'interno di un parent
// di raggio 1. Quantita per tier = sequenza Fibonacci (Cosmo: 1+1+2+3+5+8=20,
// Codex: 1+1+2+3+5=12). Caller passa TIERS calibrati per la sua densita.
//
// Strategia: primo cerchio sul bordo destro del parent, poi per ogni successivo
// prova 90 posizioni tangenti (step 4°) attorno a quelli gia posizionati e
// sceglie la piu vicina al centro (packing compatto). Fallback grid-scan per
// micro-cerchi che non trovano slot tangenti.

const PHI = 1.6180339887;

/** @public */
export const PHI_GOLDEN_RATIO = PHI;

/** @public */
export interface FibTier {
  readonly count: number;
  readonly ratio: number;
}

/** @public */
export interface PackedCircle {
  readonly r: number;
  readonly x: number;
  readonly y: number;
  readonly tier: number;
  readonly fib: number;
}

interface PackItem {
  readonly r: number;
  readonly tier: number;
}

const PARENT_R = 1;
const DEFAULT_MARGIN = 0.97;
const OVERLAP_EPSILON = 0.002;

function buildQueue(tiers: readonly FibTier[]): PackItem[] {
  const queue: PackItem[] = [];
  tiers.forEach((t, idx) => {
    for (let k = 0; k < t.count; k++) queue.push({ r: t.ratio * PARENT_R, tier: idx });
  });
  return queue;
}

function buildCircle(
  r: number,
  x: number,
  y: number,
  tier: number,
  unit: number,
): PackedCircle {
  return { r, x, y, tier, fib: Math.max(1, Math.round(r / unit)) };
}

function isInsideParent(x: number, y: number, r: number, margin: number): boolean {
  return Math.hypot(x, y) + r <= PARENT_R * margin;
}

function overlapsAny(
  x: number,
  y: number,
  r: number,
  circles: readonly PackedCircle[],
  skip: PackedCircle | null,
): boolean {
  for (const o of circles) {
    if (o === skip) continue;
    if (Math.hypot(x - o.x, y - o.y) < o.r + r - OVERLAP_EPSILON) return true;
  }
  return false;
}

function findTangentSlot(
  r: number,
  tier: number,
  circles: readonly PackedCircle[],
  unit: number,
  margin: number,
): PackedCircle | null {
  let best: PackedCircle | null = null;
  let bestScore = Infinity;
  for (const c of circles) {
    const d = c.r + r;
    for (let step = 0; step < 360; step += 4) {
      const ang = (step * Math.PI) / 180;
      const cx = c.x + Math.cos(ang) * d;
      const cy = c.y + Math.sin(ang) * d;
      if (!isInsideParent(cx, cy, r, margin)) continue;
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

function findGridSlot(
  r: number,
  tier: number,
  circles: readonly PackedCircle[],
  unit: number,
  margin: number,
): PackedCircle | null {
  const step = r * 0.6;
  for (let cx = -PARENT_R + r; cx <= PARENT_R - r; cx += step) {
    for (let cy = -PARENT_R + r; cy <= PARENT_R - r; cy += step) {
      if (!isInsideParent(cx, cy, r, margin)) continue;
      if (overlapsAny(cx, cy, r, circles, null)) continue;
      return buildCircle(r, cx, cy, tier, unit);
    }
  }
  return null;
}

/**
 * Packing Fibonacci deterministic. Coordinate normalizzate a parent_r = 1.
 * Caller moltiplica per `parentR * margin` per coordinate finali assolute.
 *
 * @param tiers FibTier[] in ordine decrescente di radius
 * @param margin frazione padding parent (default 0.97; Cosmo/Codex usano 0.92
 *   in render passandola al moltiplicatore esterno, qui 0.97 di sicurezza)
 * @public
 */
export function packFibonacci(
  tiers: readonly FibTier[],
  margin: number = DEFAULT_MARGIN,
): readonly PackedCircle[] {
  if (tiers.length === 0) return [];
  const queue = buildQueue(tiers);
  const first = queue.shift();
  if (!first) return [];
  const unit = tiers[tiers.length - 1].ratio;
  const circles: PackedCircle[] = [
    buildCircle(first.r, PARENT_R * margin - first.r, 0, first.tier, unit),
  ];

  for (const item of queue) {
    const slot =
      findTangentSlot(item.r, item.tier, circles, unit, margin) ??
      findGridSlot(item.r, item.tier, circles, unit, margin);
    if (slot) circles.push(slot);
  }

  return circles;
}

/**
 * Tier Cosmo: 6 tier 20 cerchi (1+1+2+3+5+8). Usato da satellitesFibonacci
 * dopo refactor Cosmo (TODO prossimo PR — non ancora consumato).
 * @public @lintignore
 */
export const COSMO_TIERS: readonly FibTier[] = [
  { count: 1, ratio: 1 / PHI },
  { count: 1, ratio: 1 / (PHI * PHI) },
  { count: 2, ratio: 1 / Math.pow(PHI, 3) },
  { count: 3, ratio: 1 / Math.pow(PHI, 4) },
  { count: 5, ratio: 1 / Math.pow(PHI, 5) },
  { count: 8, ratio: 1 / Math.pow(PHI, 6) },
];

/**
 * Tier Codex: 5 tier 12 cerchi (1+1+2+3+5). Una funzione satellite per ogni
 * top-function rilevante di un modulo. Meno densita di Cosmo perche le
 * funzioni interessanti per modulo sono tipicamente < 12.
 * @public
 */
export const CODEX_TIERS: readonly FibTier[] = [
  { count: 1, ratio: 1 / PHI },
  { count: 1, ratio: 1 / (PHI * PHI) },
  { count: 2, ratio: 1 / Math.pow(PHI, 3) },
  { count: 3, ratio: 1 / Math.pow(PHI, 4) },
  { count: 5, ratio: 1 / Math.pow(PHI, 5) },
];
