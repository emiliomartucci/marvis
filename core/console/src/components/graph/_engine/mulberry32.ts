// v1.0.0 - 2026-05-17 - Engine condiviso Cosmo+Codex (PR unify).
// Canonical Tommy Ettinger PRNG. Math.imul obbligatorio per semantica 32 bit
// cross-engine (V8/JSC/SpiderMonkey). Stesso seed = stesso stream.
// Originariamente in cosmo/layouts/forceLayoutHelpers.ts.

/** @public */
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
