// v1.0.0 - 2026-04-24 - Beautify layout "Constellation" (PR #1 foundation).
//
// Orbite concentriche per depth BFS da `marvisx` (root).
// Orbit 0: marvisx. Orbit 1: vicini diretti. Orbit 2+: depth successivi.
// Nodi irraggiungibili -> depth 3 (ultima orbita).
//
// Porta letterale di reference-graph-v1-cosmo.html righe 240-283.
// Centro anchorato a LAYOUT_VIEWPORT/2 per coerenza con gli altri layout.

import { resolveOverlaps, type OverlapItem } from "../../_engine";
import type { Edge, Override, Project } from "../types";
import { computeProjectRadii, LAYOUT_VIEWPORT } from "./forceLayoutHelpers";

const RINGS = [0, 260, 460, 640] as const;

/** Adiacenza non direzionata derivata dagli edge. */
function buildAdjacency(edges: readonly Edge[]): Record<string, string[]> {
  const adj: Record<string, string[]> = {};
  for (const e of edges) {
    const bucketA = adj[e.source] ?? [];
    bucketA.push(e.target);
    adj[e.source] = bucketA;
    const bucketB = adj[e.target] ?? [];
    bucketB.push(e.source);
    adj[e.target] = bucketB;
  }
  return adj;
}

/** BFS depth partendo da `marvisx`. Progetti irraggiungibili -> depth 3. */
function computeDepths(
  projects: readonly Project[],
  adj: Record<string, string[]>,
): Record<string, number> {
  const depth: Record<string, number> = { marvisx: 0 };
  const queue: string[] = ["marvisx"];
  while (queue.length) {
    const n = queue.shift();
    if (!n) break;
    for (const m of adj[n] ?? []) {
      if (depth[m] === undefined) {
        depth[m] = depth[n] + 1;
        queue.push(m);
      }
    }
  }
  for (const p of projects) {
    if (depth[p.slug] === undefined) depth[p.slug] = 3;
  }
  return depth;
}

/** Raggruppa progetti per depth. */
function groupByDepth(
  projects: readonly Project[],
  depth: Record<string, number>,
): Record<number, Project[]> {
  const byDepth: Record<number, Project[]> = {};
  for (const p of projects) {
    const d = depth[p.slug];
    const bucket = byDepth[d] ?? [];
    bucket.push(p);
    byDepth[d] = bucket;
  }
  return byDepth;
}

/**
 * Produce overrides {slug: {x, y}} per disposizione a orbite concentriche.
 * Deterministic: stesso input -> stesso output.
 * @public
 */
export function layoutConstellation(
  projects: readonly Project[],
  edges: readonly Edge[],
): Record<string, Override> {
  const cx = LAYOUT_VIEWPORT.w / 2;
  const cy = LAYOUT_VIEWPORT.h / 2;

  const adj = buildAdjacency(edges);
  const depth = computeDepths(projects, adj);
  const byDepth = groupByDepth(projects, depth);

  const radii = computeProjectRadii(projects);
  const items: Array<OverlapItem & { slug: string }> = [];
  for (const [dStr, nodes] of Object.entries(byDepth)) {
    const d = Number(dStr);
    const ring = RINGS[Math.min(RINGS.length - 1, d)];
    if (ring === 0) {
      const p = nodes[0];
      items.push({ slug: p.slug, x: cx, y: cy, r: radii.get(p.slug) ?? 22 });
      continue;
    }
    // Ordine degree-desc = gerarchia visuale all'interno dell'orbita.
    const sorted = [...nodes].sort((a, b) => b.degree - a.degree);
    const angStep = (Math.PI * 2) / sorted.length;
    const angOffset = d * 0.18; // rotazione lieve per orbita
    sorted.forEach((p, i) => {
      const ang = i * angStep + angOffset - Math.PI / 2;
      items.push({
        slug: p.slug,
        x: cx + Math.cos(ang) * ring,
        y: cy + Math.sin(ang) * ring,
        r: radii.get(p.slug) ?? 22,
      });
    });
  }

  // Final pass: zero overlap garantito anche su orbita densa (commit cosmo
  // d2aaca9 esteso al deterministic layout 2026-05-17).
  resolveOverlaps(items, { minGap: 6, passes: 15 });

  const overrides: Record<string, Override> = {};
  for (const it of items) overrides[it.slug] = { x: it.x, y: it.y };
  return overrides;
}
