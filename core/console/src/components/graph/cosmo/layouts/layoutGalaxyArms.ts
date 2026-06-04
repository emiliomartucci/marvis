// v1.0.0 - 2026-04-24 - Beautify layout "Galaxy Arms" (PR #1 foundation).
//
// Spirale logaritmica per program: ogni program occupa un braccio, nodi
// ordinati degree-desc lungo il braccio. `marvisx` rimane al centro comunque.
//
// Porta letterale di reference-graph-v1-cosmo.html righe 285-307.

import { resolveOverlaps, type OverlapItem } from "../../_engine";
import type { Override, Project } from "../types";
import { computeProjectRadii, LAYOUT_VIEWPORT } from "./forceLayoutHelpers";

/**
 * Produce overrides {slug: {x, y}} per disposizione a bracci spirale.
 * Deterministic.
 * @public
 */
export function layoutGalaxyArms(
  projects: readonly Project[],
): Record<string, Override> {
  const cx = LAYOUT_VIEWPORT.w / 2;
  const cy = LAYOUT_VIEWPORT.h / 2;

  const byProgram: Record<string, Project[]> = {};
  for (const p of projects) {
    const bucket = byProgram[p.program] ?? [];
    bucket.push(p);
    byProgram[p.program] = bucket;
  }

  // Programs sorted by size desc (il program piu' grande occupa il primo braccio).
  const programs = Object.keys(byProgram).sort(
    (a, b) => byProgram[b].length - byProgram[a].length,
  );
  const armCount = programs.length;
  const radii = computeProjectRadii(projects);
  const items: Array<OverlapItem & { slug: string }> = [];

  programs.forEach((prog, armIdx) => {
    const nodes = [...byProgram[prog]].sort((a, b) => b.degree - a.degree);
    const armRot = (armIdx / armCount) * Math.PI * 2;
    nodes.forEach((p, i) => {
      if (p.slug === "marvisx") {
        items.push({ slug: p.slug, x: cx, y: cy, r: radii.get(p.slug) ?? 22 });
        return;
      }
      const t = i / Math.max(1, nodes.length - 1);
      // Spirale logaritmica: r = a * exp(b * theta), stretched.
      const theta = armRot + t * Math.PI * 1.4;
      const r = 140 + Math.exp(1.6 * t) * 120;
      items.push({
        slug: p.slug,
        x: cx + Math.cos(theta) * r,
        y: cy + Math.sin(theta) * r,
        r: radii.get(p.slug) ?? 22,
      });
    });
  });

  // Final pass: zero overlap garantito anche su bracci densi (2026-05-17).
  resolveOverlaps(items, { minGap: 6, passes: 15 });

  const overrides: Record<string, Override> = {};
  for (const it of items) overrides[it.slug] = { x: it.x, y: it.y };
  return overrides;
}
