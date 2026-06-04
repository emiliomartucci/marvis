// v1.0.0 - 2026-05-17 - Helper standalone hard-push collision resolution.
//
// Estratto come export pubblico da `forceLayout.ts` per essere riusato dai
// layout deterministici (constellation, galaxy arms) che producono posizioni
// senza fase di simulazione fisica. Garantisce zero overlap iterando hard-push
// fino a convergenza o al cap `passes`.
//
// Differenze rispetto al `resolveCollisions` interno del force loop:
//   - lavora su un array generico `{x, y, r}` mutabile (no SimNode<T>)
//   - centro `cx`/`cy` opzionale: se passato, dopo l'hard-push se due nodi
//     finiscono fuori viewport il caller puo' decidere se reclampare. Qui no
//     side-effect su bounds (delegato al caller).

/** Item posizionabile mutabile (x/y mutati in place). */
export interface OverlapItem {
  x: number;
  y: number;
  readonly r: number;
}

export interface ResolveOverlapsOptions {
  /** Padding minimo fra bordi cerchi (default 2). */
  readonly minGap?: number;
  /** Numero massimo di pass (default 15). Early-exit se un pass non sposta nulla. */
  readonly passes?: number;
}

/**
 * Hard-push iterativo: per ogni coppia con `dist(centers) < r1+r2+minGap`,
 * spinge a meta strada lungo la retta di centri per separarli. Convergenza
 * garantita su grafi non degenerati (early-exit quando `moved=false`).
 *
 * Pattern letterale di `resolveCollisions` interno (commit cosmo d2aaca9).
 * @public
 */
export function resolveOverlaps(
  items: OverlapItem[],
  options: ResolveOverlapsOptions = {},
): void {
  const minGap = options.minGap ?? 2;
  const passes = options.passes ?? 15;

  for (let pass = 0; pass < passes; pass++) {
    let moved = false;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i];
        const b = items[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
        const minDist = a.r + b.r + minGap;
        if (dist < minDist) {
          const push = (minDist - dist) * 0.5;
          const ux = dx / dist;
          const uy = dy / dist;
          a.x -= ux * push;
          a.y -= uy * push;
          b.x += ux * push;
          b.y += uy * push;
          moved = true;
        }
      }
    }
    if (!moved) break;
  }
}
