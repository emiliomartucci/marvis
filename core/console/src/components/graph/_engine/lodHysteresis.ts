// v1.0.0 - 2026-05-17 - Engine condiviso Cosmo+Codex: LOD hysteresis helpers.
//
// Crossfade smooth fra livelli di dettaglio invece di on/off binary, per
// evitare flash percepito durante zoom continuo (commit cosmo `56d034c`).
// Originariamente helper hardcoded in GraphCanvas.tsx + CodexModulesCanvas.tsx.

/** @public */
/**
 * Opacita crossfade [0, 1] fra `from` ed `to` (or `from` ± fade).
 *
 * Esempio Cosmo arc-label (LOD_DEEP):
 *   lodOpacity(effSatR, 27, 30) → smooth da effSatR=27 (0) a effSatR=30 (1)
 *
 * Esempio Codex count badge:
 *   lodOpacity(effR, LOD_MID - 2, LOD_MID) → smooth fade-in attorno a soglia.
 *
 * Se `to <= from`, fallback step function on/off a `from`.
 * @public
 */
export function lodOpacity(value: number, from: number, to: number): number {
  if (to <= from) return value >= from ? 1 : 0;
  return Math.max(0, Math.min(1, (value - from) / (to - from)));
}

/** @public @lintignore */
/**
 * True se `delta` zoom non attraversa nessuna soglia LOD ne hysteresis zone.
 * Usato dai memo per skippare re-render di nodi/satelliti durante zoom continuo
 * (commit cosmo `f860c1a`, `9045f03`). `hysteresisWidth` espande la zona
 * attorno alla soglia per evitare flash.
 * @public
 */
export function crossesThreshold(
  prevEff: number,
  nextEff: number,
  thresholds: readonly number[],
  hysteresisWidth: number = 2,
): boolean {
  for (const t of thresholds) {
    const lo = t - hysteresisWidth;
    const hi = t + hysteresisWidth;
    const prevIn = prevEff >= lo && prevEff <= hi;
    const nextIn = nextEff >= lo && nextEff <= hi;
    if (prevIn || nextIn) return true;
    if ((prevEff < t && nextEff >= t) || (prevEff >= t && nextEff < t)) return true;
  }
  return false;
}

/**
 * Clamp valore in [min, max]. Pure helper riusato dai consumer.
 * @public
 */
export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
