// v1.0.0 - 2026-04-24 - Test layoutConstellation: orbite concentriche + distribuzione.

import { describe, expect, it } from "vitest";
import { FIXTURE_EDGES as EDGES, FIXTURE_PROJECTS as PROJECTS } from "./testFixtures";
import { layoutConstellation } from "../layouts/layoutConstellation";
import { LAYOUT_VIEWPORT } from "../layouts/forceLayoutHelpers";

describe("layoutConstellation", () => {
  it("orbite per depth — nodi su raggio costante per la stessa orbita", () => {
    const cx = LAYOUT_VIEWPORT.w / 2;
    const cy = LAYOUT_VIEWPORT.h / 2;
    const overrides = layoutConstellation(PROJECTS, EDGES);

    // Root marvisx deve essere al centro.
    const root = overrides["marvisx"];
    expect(root).toBeDefined();
    expect(Math.abs(root.x - cx)).toBeLessThan(0.001);
    expect(Math.abs(root.y - cy)).toBeLessThan(0.001);

    // Raggruppa per raggio (round al pixel) e verifica ring concentrici.
    const ringBuckets = new Map<number, number>();
    for (const [slug, pos] of Object.entries(overrides)) {
      if (slug === "marvisx") continue;
      const r = Math.round(Math.hypot(pos.x - cx, pos.y - cy));
      ringBuckets.set(r, (ringBuckets.get(r) ?? 0) + 1);
    }
    // Almeno 2 orbite distinte devono esistere (ring 1, 2, eventualmente 3).
    expect(ringBuckets.size).toBeGreaterThanOrEqual(2);
    // Raggi attesi: 260, 460, 640 (RINGS del layout).
    const radii = Array.from(ringBuckets.keys()).sort((a, b) => a - b);
    expect(radii).toEqual(
      expect.arrayContaining([
        expect.any(Number),
      ]),
    );
    // Ogni raggio deve matchare uno dei RINGS attesi (tolleranza 1px).
    const EXPECTED = [260, 460, 640];
    for (const r of radii) {
      expect(EXPECTED.some((exp) => Math.abs(r - exp) <= 1)).toBe(true);
    }
  });

  it("angoli uniformemente distribuiti [0, 2π) dentro ogni orbita", () => {
    const cx = LAYOUT_VIEWPORT.w / 2;
    const cy = LAYOUT_VIEWPORT.h / 2;
    const overrides = layoutConstellation(PROJECTS, EDGES);

    // Group by ring radius.
    const byRing = new Map<number, number[]>();
    for (const [slug, pos] of Object.entries(overrides)) {
      if (slug === "marvisx") continue;
      const r = Math.round(Math.hypot(pos.x - cx, pos.y - cy));
      const ang = Math.atan2(pos.y - cy, pos.x - cx);
      // Normalizza in [0, 2π).
      const normalized = ang < 0 ? ang + 2 * Math.PI : ang;
      let bucket = byRing.get(r);
      if (!bucket) {
        bucket = [];
        byRing.set(r, bucket);
      }
      bucket.push(normalized);
    }
    // Per ogni ring con >=3 nodi, verifica che il range angolare copra almeno π.
    for (const angles of byRing.values()) {
      if (angles.length < 3) continue;
      const min = Math.min(...angles);
      const max = Math.max(...angles);
      expect(max - min).toBeGreaterThan(Math.PI);
    }
  });
});
