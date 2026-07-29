// Smoke test for layoutGalaxyArms: shape + containment.
//
// The layoutForceFresh block was removed: it imported
// ../layouts/layoutForceFresh, a module that does not exist in this tree. The
// file did not compile, and no CI job ran this suite.

import { describe, expect, it } from "vitest";
import { FIXTURE_PROJECTS as PROJECTS } from "./testFixtures";
import { LAYOUT_VIEWPORT } from "../layouts/forceLayoutHelpers";
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
