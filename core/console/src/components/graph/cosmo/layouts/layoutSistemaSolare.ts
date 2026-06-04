// v1.0.0 - 2026-05-17 - Beautify preset "Sistema solare" (orbite larghe).
//
// Force-directed con params che producono un layout sparso, orbite larghe fra
// pianeti, ogni nodo respira. Buono per leggere i nomi e le edge senza
// confusione visiva. Era il preset Codex originario prima del PR3 unify
// (REPULSE=7800, SPRING_L=130). Zero overlap garantito da
// finalCollisionPasses=15 + MIN_GAP=12.
"use client";

import {
  forceLayout as engineForceLayout,
  type ForceEdge,
  type ForceNode,
} from "../../_engine";
import type { Edge, Override, Project } from "../types";
import { computeProjectRadii, LAYOUT_VIEWPORT } from "./forceLayoutHelpers";

type SolareNode = ForceNode & Project;

/**
 * Layout preset "Sistema solare": pianeti distanti, orbite larghe, zero
 * overlap garantito. Non-deterministic per design (seed casuale ogni call).
 * @public
 */
export function layoutSistemaSolare(
  projects: readonly Project[],
  edges: readonly Edge[] = [],
): Record<string, Override> {
  const radii = computeProjectRadii(projects);
  const nodes: SolareNode[] = projects.map((p) => ({
    ...p,
    id: p.slug,
    r: radii.get(p.slug) ?? 22,
  }));
  const forceEdges: ForceEdge[] = edges.map((e) => ({
    source: e.source,
    target: e.target,
    weight: e.weight,
  }));

  // eslint-disable-next-line sonarjs/pseudo-random
  const seed = 1000 + Math.floor(Math.random() * 9000);

  const placed = engineForceLayout<SolareNode>(nodes, forceEdges, {
    REPULSE: 7800,
    SPRING_K: 0.022,
    SPRING_L: 130,
    GRAVITY: 0.008,
    DAMPING: 0.82,
    MIN_GAP: 12,
    iterations: 400,
    seed,
    finalCollisionPasses: 15,
    viewport: LAYOUT_VIEWPORT,
    springModulation: (w) => Math.min(1, w / 8),
    gravityScale: (r) => 1 + r / 80,
    maxStep: (cool) => 14 * cool + 2,
    seedSpacing: (i) => 140 + Math.sqrt(i) * 90,
    seedJitter: 6,
    seedOrder: (a, b) => {
      if (b.degree !== a.degree) return b.degree - a.degree;
      return a.slug.localeCompare(b.slug);
    },
  });

  const overrides: Record<string, Override> = {};
  for (const n of placed) overrides[n.slug] = { x: n.x, y: n.y };
  return overrides;
}
