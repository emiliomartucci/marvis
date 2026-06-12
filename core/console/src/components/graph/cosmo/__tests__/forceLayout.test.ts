// v1.0.0 - 2026-04-24 - Test forceLayout: determinismo + convergenza + bbox + no-overlap.

import { describe, expect, it } from "vitest";
import { FIXTURE_EDGES as EDGES, FIXTURE_PROJECTS as PROJECTS } from "./testFixtures";
import { forceLayout } from "../layouts/forceLayout";
import { LAYOUT_VIEWPORT, MIN_GAP } from "../layouts/forceLayoutHelpers";

describe("forceLayout", () => {
  it("determinism — stesso input produce stesso output 5 volte", () => {
    const reference = forceLayout(PROJECTS, LAYOUT_VIEWPORT, EDGES);
    const referenceJson = JSON.stringify(reference);
    for (let i = 0; i < 4; i++) {
      const run = forceLayout(PROJECTS, LAYOUT_VIEWPORT, EDGES);
      expect(JSON.stringify(run)).toBe(referenceJson);
    }
  });

  it("convergence — delta posizione per step finale ragionevole (< 8px per nodo)", () => {
    // Proxy per "velocita' < 1" senza esporre vx/vy: delta fra run 320 e 321
    // iterazioni piccolo (damping 0.82 fa convergere). Tolleranza 8px perche'
    // il reference non clampa boundary e alcuni nodi periferici oscillano
    // piu' degli interni (questo non e' un bug: pan/zoom user compensa).
    const run320 = forceLayout(PROJECTS, LAYOUT_VIEWPORT, EDGES, 320);
    const run321 = forceLayout(PROJECTS, LAYOUT_VIEWPORT, EDGES, 321);
    const by320 = new Map(run320.map((n) => [n.slug, n]));
    for (const n of run321) {
      const prev = by320.get(n.slug);
      expect(prev).toBeDefined();
      if (!prev) continue;
      const dx = n.x - prev.x;
      const dy = n.y - prev.y;
      const delta = Math.sqrt(dx * dx + dy * dy);
      expect(delta).toBeLessThan(8);
    }
  });

  it("bbox sanity — nodi dentro il viewport allargato (no fuga < -200 o > viewport + 200)", () => {
    // Il reference non clampa (commento esplicito: "No boundary clamp — graph
    // extent is free. Pan/zoom handles exploration."). Il test verifica solo
    // che nessun nodo "scappi" lontano dal frame — la gravita' tiene il grafo
    // entro una corona generosa attorno al viewport nominale.
    const placed = forceLayout(PROJECTS, LAYOUT_VIEWPORT, EDGES);
    const slack = 200;
    const minX = -slack;
    const maxX = LAYOUT_VIEWPORT.w + slack;
    const minY = -slack;
    const maxY = LAYOUT_VIEWPORT.h + slack;
    for (const n of placed) {
      expect(n.x).toBeGreaterThanOrEqual(minX);
      expect(n.x).toBeLessThanOrEqual(maxX);
      expect(n.y).toBeGreaterThanOrEqual(minY);
      expect(n.y).toBeLessThanOrEqual(maxY);
    }
  });

  it("zero overlap — dist(ni, nj) >= ri + rj + MIN_GAP - 0.5", () => {
    const placed = forceLayout(PROJECTS, LAYOUT_VIEWPORT, EDGES);
    for (let i = 0; i < placed.length; i++) {
      for (let j = i + 1; j < placed.length; j++) {
        const a = placed[i];
        const b = placed[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const minDist = a.r + b.r + MIN_GAP - 0.5;
        expect(dist).toBeGreaterThanOrEqual(minDist);
      }
    }
  });
});
