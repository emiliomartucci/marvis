// v1.0.0 - 2026-05-17 - Engine force-directed generico (Cosmo+Codex unify).
//
// Loop fisico riusabile fra qualunque graph view: progetto (Cosmo), modulo
// (Codex), o nuovi lens futuri. Caller passa parametri calibrati per il suo
// dominio + nodi/edge minimal.
//
// Differenze rispetto al vecchio cosmo/layouts/forceLayout.ts:
//  - generic su T extends ForceNode (no piu' tipo Project hardcoded)
//  - parametri tutti in ForceParams (no constants di modulo)
//  - opzionali springModulation / gravityScale / maxStep / seedSpacing per
//    customizzare il modello senza fork
//  - resolveCollisions configurable via finalCollisionPasses (default 5)
//
// Integration Velocity-Verlet in-place + exponential cooling tramite cool =
// 1 - it/iterations. Determinism garantito da seed + mulberry32.

import { mulberry32 } from "./mulberry32";

/** Nodo input minimal — il caller estende con i suoi campi. @public */
export interface ForceNode {
  readonly id: string;
  readonly r: number;
}

/** Edge minimal — peso continuo, source/target via id. @public */
export interface ForceEdge {
  readonly source: string;
  readonly target: string;
  readonly weight: number;
}

/** Posizione assoluta dopo il layout (caller riceve T & PlacedXY). @public */
export interface PlacedXY {
  readonly x: number;
  readonly y: number;
}

/** Parametri calibrati per dominio. Tutti i default fra parentesi sono Cosmo. @public */
export interface ForceParams<T extends ForceNode = ForceNode> {
  /** Repulsione coulombiana fra coppie (Cosmo: 3500, Codex: 7800). */
  readonly REPULSE: number;
  /** Rigidita molle edge (Cosmo: 0.05, Codex: 0.022). */
  readonly SPRING_K: number;
  /** Rest length molle (Cosmo: 18, Codex: 130). */
  readonly SPRING_L: number;
  /** Tira verso il centro (Cosmo: 0.022, Codex: 0.008). */
  readonly GRAVITY: number;
  /** Damping velocity (Cosmo+Codex: 0.82). */
  readonly DAMPING: number;
  /** Padding minimo fra cerchi (Cosmo: 4, Codex: 12). */
  readonly MIN_GAP: number;
  /** Numero step fisici (default 300). */
  readonly iterations: number;
  /** Seed PRNG per determinismo (Cosmo: 42, Codex: 73). */
  readonly seed: number;
  /** Centro gravity + layout coords. */
  readonly viewport: { readonly w: number; readonly h: number };

  /**
   * Modulazione force della molla in base al peso edge (default min(1, w/6)).
   * Cosmo usa min(1, w/6), Codex min(1, w/8).
   */
  readonly springModulation?: (weight: number) => number;
  /**
   * Scala gravity in base al raggio nodo (default 1 + r/60).
   * Cosmo: 1 + r/60. Codex: 1 + r/80.
   */
  readonly gravityScale?: (r: number) => number;
  /**
   * Max step per integrazione (default 12 * cool + 2).
   */
  readonly maxStep?: (cool: number) => number;
  /**
   * Distanza seed iniziale per nodo i-esimo (default 80 + sqrt(i) * 60).
   * Cosmo: 80 + sqrt(i)*60. Codex: 140 + sqrt(i)*90.
   */
  readonly seedSpacing?: (i: number) => number;
  /** Ampiezza jitter seed (default 4). */
  readonly seedJitter?: number;
  /** Pass finali di hard-push collision resolution (default 5). */
  readonly finalCollisionPasses?: number;
  /**
   * Ordine seed: rank 0 al centro. Tie-break stabile via id (default
   * desc r → asc id). Override per ordinare per degree esterno.
   */
  readonly seedOrder?: (a: T, b: T) => number;
}

interface SimNode<T extends ForceNode> {
  readonly node: T;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

const GOLDEN_ANGLE = 2.399963;

function defaultSpringModulation(w: number): number {
  return Math.min(1, w / 6);
}

function defaultGravityScale(r: number): number {
  return 1 + r / 60;
}

function defaultMaxStep(cool: number): number {
  return 12 * cool + 2;
}

function defaultSeedSpacing(i: number): number {
  return 80 + Math.sqrt(i) * 60;
}

function defaultSeedOrder<T extends ForceNode>(a: T, b: T): number {
  if (b.r !== a.r) return b.r - a.r;
  return a.id.localeCompare(b.id);
}

function seedNodes<T extends ForceNode>(
  nodes: readonly T[],
  params: ForceParams<T>,
): SimNode<T>[] {
  const rand = mulberry32(params.seed);
  const cx = params.viewport.w / 2;
  const cy = params.viewport.h / 2;
  const sorted = [...nodes].sort(params.seedOrder ?? defaultSeedOrder);
  const spacing = params.seedSpacing ?? defaultSeedSpacing;
  const jitter = params.seedJitter ?? 4;

  return sorted.map((n, i) => {
    if (i === 0) return { node: n, x: cx, y: cy, vx: 0, vy: 0 };
    const ang = i * GOLDEN_ANGLE;
    const dist = spacing(i);
    return {
      node: n,
      x: cx + Math.cos(ang) * dist + (rand() - 0.5) * jitter,
      y: cy + Math.sin(ang) * dist + (rand() - 0.5) * jitter,
      vx: 0,
      vy: 0,
    };
  });
}

function applyPairRepulsion<T extends ForceNode>(
  a: SimNode<T>,
  b: SimNode<T>,
  params: ForceParams<T>,
): void {
  let dx = b.x - a.x;
  let dy = b.y - a.y;
  let dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
  const minDist = a.node.r + b.node.r + params.MIN_GAP;
  if (dist < minDist) {
    const push = (minDist - dist) * 0.5;
    const ux = dx / dist;
    const uy = dy / dist;
    a.x -= ux * push;
    a.y -= uy * push;
    b.x += ux * push;
    b.y += uy * push;
    dx = b.x - a.x;
    dy = b.y - a.y;
    dist = minDist;
  }
  const force = (params.REPULSE * (a.node.r + b.node.r)) / (dist * dist);
  const ux = dx / dist;
  const uy = dy / dist;
  a.vx -= ux * force;
  a.vy -= uy * force;
  b.vx += ux * force;
  b.vy += uy * force;
}

function applyEdgeSpring<T extends ForceNode>(
  a: SimNode<T>,
  b: SimNode<T>,
  weight: number,
  params: ForceParams<T>,
): void {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
  const rest = params.SPRING_L + (a.node.r + b.node.r);
  const displacement = dist - rest;
  const mod = (params.springModulation ?? defaultSpringModulation)(weight);
  const k = params.SPRING_K * mod;
  const fx = (dx / dist) * displacement * k;
  const fy = (dy / dist) * displacement * k;
  a.vx += fx;
  a.vy += fy;
  b.vx -= fx;
  b.vy -= fy;
}

function integrateNode<T extends ForceNode>(
  n: SimNode<T>,
  cx: number,
  cy: number,
  cool: number,
  params: ForceParams<T>,
): void {
  const gScale = (params.gravityScale ?? defaultGravityScale)(n.node.r);
  n.vx += (cx - n.x) * params.GRAVITY * gScale;
  n.vy += (cy - n.y) * params.GRAVITY * gScale;
  n.vx *= params.DAMPING;
  n.vy *= params.DAMPING;
  const maxStep = (params.maxStep ?? defaultMaxStep)(cool);
  n.vx = Math.max(-maxStep, Math.min(maxStep, n.vx));
  n.vy = Math.max(-maxStep, Math.min(maxStep, n.vy));
  n.x += n.vx;
  n.y += n.vy;
}

function resolveCollisions<T extends ForceNode>(
  nodes: SimNode<T>[],
  params: ForceParams<T>,
): void {
  const passes = params.finalCollisionPasses ?? 5;
  for (let pass = 0; pass < passes; pass++) {
    let moved = false;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.1;
        const minDist = a.node.r + b.node.r + params.MIN_GAP;
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

/**
 * Force-directed deterministic. Loop O(n^2) per iterazione: ok per n <= ~150
 * (Cosmo: 28-81 progetti; Codex: 12-30 moduli). Per scale maggiori sostituire
 * con Barnes-Hut quad-tree.
 *
 * Ritorna `(T & PlacedXY)[]` — caller mantiene tutti i campi originali +
 * coordinate. Strippa vx/vy interni.
 *
 * @public
 */
export function forceLayout<T extends ForceNode>(
  nodes: readonly T[],
  edges: readonly ForceEdge[],
  params: ForceParams<T>,
): Array<T & PlacedXY> {
  const cx = params.viewport.w / 2;
  const cy = params.viewport.h / 2;

  const simNodes = seedNodes(nodes, params);
  const byId: Record<string, SimNode<T>> = {};
  for (const s of simNodes) byId[s.node.id] = s;
  const edgeList = edges.filter((e) => byId[e.source] && byId[e.target]);

  for (let it = 0; it < params.iterations; it++) {
    const cool = 1 - it / params.iterations;
    for (let i = 0; i < simNodes.length; i++) {
      for (let j = i + 1; j < simNodes.length; j++) {
        applyPairRepulsion(simNodes[i], simNodes[j], params);
      }
    }
    for (const e of edgeList) {
      applyEdgeSpring(byId[e.source], byId[e.target], e.weight, params);
    }
    for (const n of simNodes) integrateNode(n, cx, cy, cool, params);
  }

  resolveCollisions(simNodes, params);

  return simNodes.map((s) => ({ ...s.node, x: s.x, y: s.y }));
}
