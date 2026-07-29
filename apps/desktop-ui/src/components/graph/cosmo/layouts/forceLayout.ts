// v2.0.0 - 2026-05-17 - Refactor a engine condiviso (PR2 unify Cosmo+Codex).
//
// Era loop fisico copia-incolla da reference-graph-v1-cosmo.html righe 142-232.
// Dopo PR1 (commit 6b8713c) abbiamo `console/src/components/graph/_engine/`:
// il loop e' generico, qui resta solo l'adattatore Cosmo-specific che:
//   - mappa Project → ForceNode (id=slug, r calcolato via computeProjectRadii)
//   - passa params "grappolo d'uva v5" calibrati per super-nodo progetto
//   - mantiene ordine seed degree-based (era: r-based default engine)
//   - strippa il campo `id` post-layout per restituire `PlacedNode` puro
//
// ZERO behavior change vs v1 — i parametri qui sono gli stessi che il vecchio
// loop usava come constants del modulo. Il default `springModulation`/
// `gravityScale`/`maxStep`/`seedSpacing`/`seedJitter`/`finalCollisionPasses`
// dell'engine coincide con i valori Cosmo, quindi non servono override per
// quei callback.

import {
  forceLayout as engineForceLayout,
  type ForceEdge,
  type ForceNode,
} from "../../_engine";
import type { Edge, PlacedNode, Project } from "../types";
import { MIN_GAP, computeProjectRadii } from "./forceLayoutHelpers";

// Params "super grappolo no-overlap" (2026-05-17 — Emilio: "+grappolo possibile
// ma senza sovrapposizioni di aree tra i cerchi"). Vs v5 grappolo:
//   - REPULSE 3500→2000 (meno repulsione → cluster ancora piu denso)
//   - SPRING_K 0.05→0.07 (molle piu rigide → correlati ancora piu vicini)
//   - SPRING_L 18→10 (rest length piu corto → cerchi quasi tangenti)
//   - GRAVITY 0.022→0.030 (piu forte verso centro → meno drift periferico)
//   - MIN_GAP 4→2 (padding minimo → cerchi piu serrati)
//   - iterations 320→400 (piu step → convergenza solida)
//   - finalCollisionPasses 5→15 (3x pass hard-push finale → ZERO overlap
//     garantito anche in cluster densissimi)
// Default engine springModulation/gravityScale/maxStep/seedSpacing/seedJitter
// invariati (sono gia ottimi per cluster denso, definiti per Cosmo).
const COSMO_FORCE_PARAMS = {
  REPULSE: 2000,
  SPRING_K: 0.07,
  SPRING_L: 10,
  GRAVITY: 0.030,
  DAMPING: 0.82,
  MIN_GAP: 2,
  iterations: 400,
  seed: 42,
  finalCollisionPasses: 15,
} as const;

/** Project + id (alias slug) per soddisfare ForceNode constraint. */
type CosmoForceNode = ForceNode & Project;

/**
 * Esegue la simulazione force-directed Cosmo deterministic via engine.
 * @public
 */
export function forceLayout(
  projects: readonly Project[],
  viewport: { w: number; h: number },
  edges: readonly Edge[] = [],
  // Typed explicitly: COSMO_FORCE_PARAMS is `as const`, so inferring from
  // the default narrowed this to the literal 400 and rejected every other
  // iteration count callers pass.
  iterations: number = COSMO_FORCE_PARAMS.iterations,
): PlacedNode[] {
  const radii = computeProjectRadii(projects);
  const nodes: CosmoForceNode[] = projects.map((p) => ({
    ...p,
    id: p.slug,
    r: radii.get(p.slug) ?? 22,
  }));
  const forceEdges: ForceEdge[] = edges.map((e) => ({
    source: e.source,
    target: e.target,
    weight: e.weight,
  }));

  const placed = engineForceLayout<CosmoForceNode>(nodes, forceEdges, {
    ...COSMO_FORCE_PARAMS,
    iterations,
    viewport,
    // Ordine seed degree-desc (era seedNodes Cosmo): pianeta piu connesso
    // al centro, tie-break stabile su slug.
    seedOrder: (a, b) => {
      if (b.degree !== a.degree) return b.degree - a.degree;
      return a.slug.localeCompare(b.slug);
    },
  });

  // Strippa il campo `id` ausiliario — il consumer vede solo PlacedNode.
  return placed.map<PlacedNode>((n) => ({
    slug: n.slug,
    program: n.program,
    degree: n.degree,
    satellites: n.satellites,
    r: n.r,
    x: n.x,
    y: n.y,
  }));
}
