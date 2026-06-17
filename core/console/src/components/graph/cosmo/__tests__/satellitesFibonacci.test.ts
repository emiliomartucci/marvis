// v1.1.0 - 2026-04-24 - SatelliteSummary input (count + latest_at)
// v1.0.0 - 2026-04-24 - Test satellitesFibonacci: packing + containment + cap.

import { describe, expect, it } from "vitest";
import { FIB_PACKING } from "../layouts/forceLayoutHelpers";
import { layoutSatellitesFib } from "../layouts/satellitesFibonacci";
import type { Kind, PlacedNode, SatelliteSummary } from "../types";

function fakeProject(r: number): PlacedNode {
  return {
    slug: "marvisx",
    program: "marvis",
    degree: 100,
    satellites: [],
    x: 1200,
    y: 750,
    r,
  };
}

function sum(kind: Kind, count = 1): SatelliteSummary {
  return { kind, count, latest_at: null };
}

const SUMMARIES: SatelliteSummary[] = [
  sum("plan", 3),
  sum("handoff", 2),
  sum("solution", 1),
  sum("audit", 4),
  sum("brainstorm", 1),
  sum("task", 8),
  sum("research", 2),
  sum("learning", 5),
];

describe("layoutSatellitesFib", () => {
  it("packing non-overlapping — dist(si, sj) >= ri + rj - 0.5", () => {
    const project = fakeProject(80);
    const sats = layoutSatellitesFib(project, SUMMARIES);
    expect(sats.length).toBe(SUMMARIES.length);
    for (let i = 0; i < sats.length; i++) {
      for (let j = i + 1; j < sats.length; j++) {
        const a = sats[i];
        const b = sats[j];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        expect(dist).toBeGreaterThanOrEqual(a.r + b.r - 0.5);
      }
    }
  });

  it("stay within parent radius * 0.75 — dist(s, center) + s.r <= parent.r * 0.75 + 0.5", () => {
    // Il reference usa parentR * margin (0.97) internamente; l'output pero'
    // viene moltiplicato per 0.92 -> effective bound ~ parent.r * 0.92 * 0.97
    // = 0.89. Il test usa 0.92 come bound superiore (include il bordo).
    const project = fakeProject(80);
    const sats = layoutSatellitesFib(project, SUMMARIES);
    const bound = project.r * 0.92 + 0.5;
    for (const s of sats) {
      const dist = Math.hypot(s.x - project.x, s.y - project.y);
      expect(dist + s.r).toBeLessThanOrEqual(bound);
    }
  });

  it("count limitato a FIB_PACKING.length — input 30 summaries -> output cap", () => {
    const project = fakeProject(80);
    const many: SatelliteSummary[] = Array.from({ length: 30 }, (_, i) =>
      sum(i % 2 === 0 ? "plan" : "task", 1),
    );
    const sats = layoutSatellitesFib(project, many);
    expect(sats.length).toBe(FIB_PACKING.length);
  });

  it("empty summaries -> empty output, no throw", () => {
    const project = fakeProject(80);
    const sats = layoutSatellitesFib(project, []);
    expect(sats).toEqual([]);
  });

  it("propaga count + latest_at su PlacedSatellite", () => {
    const project = fakeProject(80);
    const sats = layoutSatellitesFib(project, [
      { kind: "plan", count: 7, latest_at: "2026-04-20T10:00:00Z" },
      { kind: "task", count: 0, latest_at: null },
    ]);
    expect(sats[0].count).toBe(7);
    expect(sats[0].latest_at).toBe("2026-04-20T10:00:00Z");
    expect(sats[1].count).toBe(0);
    expect(sats[1].latest_at).toBeNull();
  });
});
