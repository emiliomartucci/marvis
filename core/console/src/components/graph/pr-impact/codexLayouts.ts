// v1.0.0 - 2026-05-17 - Codex Beautify presets (PR4 unify).
//
// Mirror dei layout Cosmo (layoutGrappolo + layoutSistemaSolare) ma tipizzati
// per CodexModuleItem (moduli) invece di Project. Usano lo stesso engine
// force-directed condiviso `_engine/forceLayout`.

"use client";

import {
  forceLayout as engineForceLayout,
  resolveOverlaps,
  type ForceEdge,
  type ForceNode,
  type OverlapItem,
} from "../_engine";
import type { CodexClusterId, CodexModuleEdgeItem, CodexModuleItem } from "./types";

const CODEX_WORLD = { w: 1800, h: 1100 } as const;

interface CodexForceNode extends ForceNode {
  readonly slug: string;
  readonly degree: number;
  readonly function_count: number;
}

function buildNodes(modules: readonly CodexModuleItem[]): CodexForceNode[] {
  return modules.map((m) => {
    const effective = m.degree > 0 ? m.degree : m.function_count / 4;
    return {
      id: m.slug,
      slug: m.slug,
      degree: m.degree,
      function_count: m.function_count,
      r: Math.max(36, Math.min(120, Math.pow(effective, 0.55) * 6.5)),
    };
  });
}

function buildEdges(edges: readonly CodexModuleEdgeItem[]): ForceEdge[] {
  return edges.map((e) => ({ source: e.source, target: e.target, weight: e.weight }));
}

function seedOrder(a: CodexForceNode, b: CodexForceNode): number {
  const ka = a.degree || a.function_count;
  const kb = b.degree || b.function_count;
  if (kb !== ka) return kb - ka;
  return a.id.localeCompare(b.id);
}

/**
 * Layout Codex "Grappolo": cluster denso, cerchi quasi tangenti, zero overlap.
 * @public
 */
export function layoutCodexGrappolo(
  modules: readonly CodexModuleItem[],
  edges: readonly CodexModuleEdgeItem[],
): Record<string, { x: number; y: number }> {
  // eslint-disable-next-line sonarjs/pseudo-random
  const seed = 1000 + Math.floor(Math.random() * 9000);
  const placed = engineForceLayout<CodexForceNode>(buildNodes(modules), buildEdges(edges), {
    REPULSE: 2000,
    SPRING_K: 0.07,
    SPRING_L: 10,
    GRAVITY: 0.030,
    DAMPING: 0.82,
    MIN_GAP: 2,
    iterations: 400,
    seed,
    finalCollisionPasses: 15,
    viewport: CODEX_WORLD,
    seedOrder,
  });
  const out: Record<string, { x: number; y: number }> = {};
  for (const n of placed) out[n.slug] = { x: n.x, y: n.y };
  return out;
}

/**
 * Layout Codex "Sistema solare": orbite larghe, pianeti distanti, zero overlap.
 * @public
 */
export function layoutCodexSistemaSolare(
  modules: readonly CodexModuleItem[],
  edges: readonly CodexModuleEdgeItem[],
): Record<string, { x: number; y: number }> {
  // eslint-disable-next-line sonarjs/pseudo-random
  const seed = 1000 + Math.floor(Math.random() * 9000);
  const placed = engineForceLayout<CodexForceNode>(buildNodes(modules), buildEdges(edges), {
    REPULSE: 7800,
    SPRING_K: 0.022,
    SPRING_L: 130,
    GRAVITY: 0.008,
    DAMPING: 0.82,
    MIN_GAP: 12,
    iterations: 400,
    seed,
    finalCollisionPasses: 15,
    viewport: CODEX_WORLD,
    springModulation: (w) => Math.min(1, w / 8),
    gravityScale: (r) => 1 + r / 80,
    maxStep: (cool) => 14 * cool + 2,
    seedSpacing: (i) => 140 + Math.sqrt(i) * 90,
    seedJitter: 6,
    seedOrder,
  });
  const out: Record<string, { x: number; y: number }> = {};
  for (const n of placed) out[n.slug] = { x: n.x, y: n.y };
  return out;
}

/** @public */
export type CodexBeautifyKind =
  | "constellation"
  | "galaxy"
  | "grappolo"
  | "sistema-solare"
  | "reset";

// ---------- Constellation: orbite concentriche per cluster ------------------

const CLUSTER_RING_ORDER: readonly CodexClusterId[] = [
  "shared", // ring 0 (centro)
  "api",    // ring 1
  "db",
  "graph",
  "ui",
  "parse",
  "search",
  "auth",
];
const CLUSTER_RINGS = [0, 240, 420, 580, 720] as const;

function radiusFromModule(m: CodexModuleItem): number {
  const effective = m.degree > 0 ? m.degree : m.function_count / 4;
  return Math.max(36, Math.min(120, Math.pow(effective, 0.55) * 6.5));
}

/**
 * Constellation Codex: orbite concentriche per cluster. Il cluster `shared`
 * occupa il centro, gli altri 7 cluster si distribuiscono su 4 anelli (4
 * cluster all'anello esterno). Zero overlap garantito.
 * @public
 */
export function layoutCodexConstellation(
  modules: readonly CodexModuleItem[],
): Record<string, { x: number; y: number }> {
  const cx = CODEX_WORLD.w / 2;
  const cy = CODEX_WORLD.h / 2;
  const byCluster = new Map<CodexClusterId, CodexModuleItem[]>();
  for (const m of modules) {
    const arr = byCluster.get(m.cluster) ?? [];
    arr.push(m);
    byCluster.set(m.cluster, arr);
  }
  const orderedClusters: CodexClusterId[] = CLUSTER_RING_ORDER.filter((c) => byCluster.has(c));
  for (const c of byCluster.keys()) {
    if (!orderedClusters.includes(c)) orderedClusters.push(c);
  }

  const items: Array<OverlapItem & { slug: string }> = [];
  orderedClusters.forEach((cluster, clusterIdx) => {
    const ring = CLUSTER_RINGS[Math.min(CLUSTER_RINGS.length - 1, clusterIdx)];
    const cmods = (byCluster.get(cluster) ?? []).sort(
      (a, b) => (b.degree || b.function_count) - (a.degree || a.function_count),
    );
    if (ring === 0) {
      cmods.forEach((m, i) => {
        if (cmods.length === 1) {
          items.push({ slug: m.slug, x: cx, y: cy, r: radiusFromModule(m) });
        } else {
          const ang = (i / cmods.length) * Math.PI * 2;
          const inner = 80;
          items.push({
            slug: m.slug,
            x: cx + Math.cos(ang) * inner,
            y: cy + Math.sin(ang) * inner,
            r: radiusFromModule(m),
          });
        }
      });
      return;
    }
    const angStep = (Math.PI * 2) / Math.max(1, cmods.length);
    const angOffset = clusterIdx * 0.22;
    cmods.forEach((m, i) => {
      const ang = i * angStep + angOffset - Math.PI / 2;
      items.push({
        slug: m.slug,
        x: cx + Math.cos(ang) * ring,
        y: cy + Math.sin(ang) * ring,
        r: radiusFromModule(m),
      });
    });
  });

  resolveOverlaps(items, { minGap: 6, passes: 20 });
  const out: Record<string, { x: number; y: number }> = {};
  for (const it of items) out[it.slug] = { x: it.x, y: it.y };
  return out;
}

// ---------- Galaxy: spirale logaritmica per cluster -------------------------

/**
 * Galaxy Codex: ogni cluster occupa un braccio spirale logaritmica. Moduli
 * ordinati degree-desc lungo il braccio. Zero overlap garantito.
 * @public
 */
export function layoutCodexGalaxy(
  modules: readonly CodexModuleItem[],
): Record<string, { x: number; y: number }> {
  const cx = CODEX_WORLD.w / 2;
  const cy = CODEX_WORLD.h / 2;
  const byCluster = new Map<CodexClusterId, CodexModuleItem[]>();
  for (const m of modules) {
    const arr = byCluster.get(m.cluster) ?? [];
    arr.push(m);
    byCluster.set(m.cluster, arr);
  }
  const clusters = Array.from(byCluster.keys()).sort(
    (a, b) => (byCluster.get(b)?.length ?? 0) - (byCluster.get(a)?.length ?? 0),
  );
  const armCount = Math.max(1, clusters.length);

  const items: Array<OverlapItem & { slug: string }> = [];
  clusters.forEach((cluster, armIdx) => {
    const cmods = (byCluster.get(cluster) ?? []).sort(
      (a, b) => (b.degree || b.function_count) - (a.degree || a.function_count),
    );
    const armRot = (armIdx / armCount) * Math.PI * 2;
    cmods.forEach((m, i) => {
      const t = i / Math.max(1, cmods.length - 1);
      const theta = armRot + t * Math.PI * 1.4;
      const r = 130 + Math.exp(1.5 * t) * 110;
      items.push({
        slug: m.slug,
        x: cx + Math.cos(theta) * r,
        y: cy + Math.sin(theta) * r,
        r: radiusFromModule(m),
      });
    });
  });

  resolveOverlaps(items, { minGap: 6, passes: 20 });
  const out: Record<string, { x: number; y: number }> = {};
  for (const it of items) out[it.slug] = { x: it.x, y: it.y };
  return out;
}
