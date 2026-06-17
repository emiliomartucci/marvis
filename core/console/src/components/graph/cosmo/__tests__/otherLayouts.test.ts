// v1.0.0 - 2026-04-24 - Smoke test per layoutGalaxyArms + layoutForceFresh.
//
// Non-deterministic per forceFresh (usa Math.random), quindi test limitati a
// shape + containment.

import { describe, expect, it } from "vitest";
import { FIXTURE_EDGES as EDGES, FIXTURE_PROJECTS as PROJECTS } from "./testFixtures";
import { LAYOUT_VIEWPORT, MIN_GAP, projectRadius } from "../layouts/forceLayoutHelpers";
import { layoutForceFresh } from "../layouts/layoutForceFresh";
import { layoutGalaxyArms } from "../layouts/layoutGalaxyArms";

describe("layoutGalaxyArms", () => {
  it("produce un override per ogni progetto", () => {
    const overrides = layoutGalaxyArms(PROJECTS);
    expect(Object.keys(overrides).length).toBe(PROJECTS.length);
    for (const p of PROJECTS) {
      expect(overrides[p.slug]).toBeDefined();
    }
  });

  it("marvisx ancorato al centro del viewport", () => {
    const overrides = layoutGalaxyArms(PROJECTS);
    const cx = LAYOUT_VIEWPORT.w / 2;
    const cy = LAYOUT_VIEWPORT.h / 2;
    const root = overrides["marvisx"];
    expect(Math.abs(root.x - cx)).toBeLessThan(0.001);
    expect(Math.abs(root.y - cy)).toBeLessThan(0.001);
  });

  it("coordinate finite (no NaN / Infinity)", () => {
    const overrides = layoutGalaxyArms(PROJECTS);
    for (const pos of Object.values(overrides)) {
      expect(Number.isFinite(pos.x)).toBe(true);
      expect(Number.isFinite(pos.y)).toBe(true);
    }
  });
});

describe("layoutForceFresh", () => {
  it("produce un override per ogni progetto con coordinate finite", () => {
    const overrides = layoutForceFresh(PROJECTS, EDGES);
    expect(Object.keys(overrides).length).toBe(PROJECTS.length);
    for (const pos of Object.values(overrides)) {
      expect(Number.isFinite(pos.x)).toBe(true);
      expect(Number.isFinite(pos.y)).toBe(true);
    }
  });

  it("due chiamate successive producono layout diversi (non-deterministic)", () => {
    const a = layoutForceFresh(PROJECTS, EDGES);
    const b = layoutForceFresh(PROJECTS, EDGES);
    // Almeno un nodo deve differire (prob. ~1 di jitter diverso).
    let differs = false;
    for (const slug of Object.keys(a)) {
      if (a[slug].x !== b[slug].x || a[slug].y !== b[slug].y) {
        differs = true;
        break;
      }
    }
    expect(differs).toBe(true);
  });

  it("zero overlap dopo final pass — dist >= ri + rj + MIN_GAP - 0.5 (10 run)", () => {
    // Garanzia post-fix bug 2: il final pass `resolveCollisions` deve
    // eliminare overlap residui anche con seed jitter casuale (10 run).
    const radii = new Map(PROJECTS.map((p) => [p.slug, projectRadius(p.degree)]));
    for (let run = 0; run < 10; run++) {
      const overrides = layoutForceFresh(PROJECTS, EDGES);
      const slugs = Object.keys(overrides);
      for (let i = 0; i < slugs.length; i++) {
        for (let j = i + 1; j < slugs.length; j++) {
          const a = overrides[slugs[i]];
          const b = overrides[slugs[j]];
          const ra = radii.get(slugs[i]) ?? 0;
          const rb = radii.get(slugs[j]) ?? 0;
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const minDist = ra + rb + MIN_GAP - 0.5;
          expect(dist).toBeGreaterThanOrEqual(minDist);
        }
      }
    }
  });
});
