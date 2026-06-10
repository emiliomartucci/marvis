// v1.0.0 - 2026-05-17 - Beautify preset "Grappolo" (super dense, zero overlap).
//
// Force-directed via engine con params "super grappolo no-overlap" — stessi
// usati dal layout iniziale Cosmo (vedi forceLayout.ts:COSMO_FORCE_PARAMS) +
// stessi di Codex CodexModulesCanvas (PR3 unify). Cluster denso con cerchi
// quasi tangenti e ZERO overlap garantito da finalCollisionPasses=15.
//
// Differenza vs forceLayout principale: jitter cosmetico Math.random per
// uscire dal minimo locale e produrre un layout "nuovo" alla pressione del
// Beautify (i seed diversi cambiano la disposizione angolare, non i params).
"use client";

import {
  forceLayout as engineForceLayout,
  type ForceEdge,
  type ForceNode,
} from "../../_engine";
import type { Edge, Override, Project } from "../types";
import { computeProjectRadii, LAYOUT_VIEWPORT } from "./forceLayoutHelpers";

type GrappoloNode = ForceNode & Project;

/**
 * Layout preset "Grappolo": cluster densissimo, cerchi quasi tangenti, zero
 * overlap garantito. Non-deterministic per design (seed casuale ogni call).
 * @public
 */
export function layoutGrappolo(
  projects: readonly Project[],
  edges: readonly Edge[] = [],
): Record<string, Override> {
  const radii = computeProjectRadii(projects);
  const nodes: GrappoloNode[] = projects.map((p) => ({
    ...p,
    id: p.slug,
    r: radii.get(p.slug) ?? 22,
  }));
  const forceEdges: ForceEdge[] = edges.map((e) => ({
    source: e.source,
    target: e.target,
    weight: e.weight,
  }));

  // Seed casuale 1000..9999 → shuffle layout fra una pressione Beautify e
  // l'altra. La fisica resta deterministic-per-seed (debug riproducibile).
  // eslint-disable-next-line sonarjs/pseudo-random
  const seed = 1000 + Math.floor(Math.random() * 9000);

  const placed = engineForceLayout<GrappoloNode>(nodes, forceEdges, {
    REPULSE: 2000,
    SPRING_K: 0.07,
    SPRING_L: 10,
    GRAVITY: 0.030,
    DAMPING: 0.82,
    MIN_GAP: 2,
    iterations: 400,
    seed,
    finalCollisionPasses: 15,
    viewport: LAYOUT_VIEWPORT,
    seedOrder: (a, b) => {
      if (b.degree !== a.degree) return b.degree - a.degree;
      return a.slug.localeCompare(b.slug);
    },
  });

  const overrides: Record<string, Override> = {};
  for (const n of placed) overrides[n.slug] = { x: n.x, y: n.y };
  return overrides;
}
