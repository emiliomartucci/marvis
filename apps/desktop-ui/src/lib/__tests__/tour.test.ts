import { describe, expect, it } from "vitest";

import {
  advanceTour,
  canAdvanceTourStep,
  currentTourStep,
  initialTourState,
  shouldSkipMissingAnchor,
  tourSteps,
  type TourMachineState,
  type TourPart,
} from "../tour";

function walk(part: TourPart): string[] {
  const steps = tourSteps(part);
  let state: TourMachineState = initialTourState(part);
  const ids: string[] = [];

  while (!state.completed) {
    const step = currentTourStep(state, steps);
    if (!step) break;
    ids.push(step.id);
    state = advanceTour(state, step.gate ? "gate" : "next", steps);
  }

  return ids;
}

describe("tour step sequences", () => {
  it("keeps part 1 in the onboarding walkthrough order", () => {
    expect(tourSteps(1).map((step) => step.id)).toEqual([
      "diario",
      "cronologia",
      "todos",
      "todo-capture",
      "todo-type",
      "todo-approva",
      "task-views",
      "task-card",
      "task-drawer",
      "project-row",
      "project-dash",
      "final",
    ]);
  });

  it("keeps part 2 focused on help-menu advanced chrome", () => {
    expect(tourSteps(2).map((step) => step.id)).toEqual([
      "universe",
      "capture",
      "theme",
      "locale",
      "final",
    ]);
  });

  it("can omit the approval step when no approval item exists", () => {
    const steps = tourSteps(1, { hasApproveTarget: false });

    expect(steps.map((step) => step.id)).not.toContain("todo-approva");
    expect(steps.map((step) => step.id)).toContain("task-card");
  });
});

describe("tour state machine", () => {
  it("starts at the first step and completes after the final card", () => {
    const steps = tourSteps(2);
    let state = initialTourState(2);

    expect(currentTourStep(state, steps)?.id).toBe("universe");

    for (const _step of steps) {
      state = advanceTour(state, "next", steps);
    }

    expect(state.completed).toBe(true);
    expect(currentTourStep(state, steps)).toBeNull();
  });

  it("does not advance gated steps on plain next", () => {
    const steps = tourSteps(1);
    let state = initialTourState(1);
    while (currentTourStep(state, steps)?.id !== "todo-approva") {
      state = advanceTour(state, "next", steps);
    }

    const gated = currentTourStep(state, steps);
    expect(gated?.gate).toBe(true);
    expect(canAdvanceTourStep(gated, "next")).toBe(false);

    const blocked = advanceTour(state, "next", steps);
    expect(blocked).toEqual(state);

    const advanced = advanceTour(state, "gate", steps);
    expect(currentTourStep(advanced, steps)?.id).toBe("task-views");
  });

  it("allows explicit skip through gated steps", () => {
    const steps = tourSteps(1);
    let state = initialTourState(1);
    while (currentTourStep(state, steps)?.id !== "task-card") {
      state = advanceTour(state, currentTourStep(state, steps)?.gate ? "gate" : "next", steps);
    }

    const skipped = advanceTour(state, "skip", steps);
    expect(currentTourStep(skipped, steps)?.id).toBe("task-drawer");
  });

  it("walks each part with its own gated events", () => {
    expect(walk(1)).toContain("project-dash");
    expect(walk(2)).toEqual(["universe", "capture", "theme", "locale", "final"]);
  });

  it("only auto-skips a missing approval anchor", () => {
    const approvalStep = tourSteps(1).find((step) => step.id === "todo-approva") ?? null;
    const taskStep = tourSteps(1).find((step) => step.id === "task-card") ?? null;

    expect(shouldSkipMissingAnchor(approvalStep)).toBe(true);
    expect(shouldSkipMissingAnchor(taskStep)).toBe(false);
    expect(shouldSkipMissingAnchor(null)).toBe(false);
  });
});
