export type TourPart = 1 | 2;

export type TourStepId =
  | "diario"
  | "cronologia"
  | "todos"
  | "todo-capture"
  | "todo-type"
  | "todo-approva"
  | "task-views"
  | "task-card"
  | "task-drawer"
  | "project-row"
  | "project-dash"
  | "universe"
  | "capture"
  | "theme"
  | "locale"
  | "final";

export interface TourStep {
  id: TourStepId;
  part: TourPart;
  route?: string;
  anchor?: string;
  gate?: boolean;
  final?: boolean;
}

export interface TourMachineState {
  part: TourPart;
  index: number;
  completed: boolean;
}

const PART_1_STEPS: TourStep[] = [
  { part: 1, id: "diario", route: "/diario/", anchor: "diario" },
  { part: 1, id: "cronologia", route: "/diario/", anchor: "cronologia" },
  { part: 1, id: "todos", route: "/todos/", anchor: "todos" },
  { part: 1, id: "todo-capture", route: "/todos/", anchor: "todo-capture" },
  { part: 1, id: "todo-type", route: "/todos/", anchor: "todo-type" },
  { part: 1, id: "todo-approva", route: "/todos/", anchor: "todo-approva", gate: true },
  { part: 1, id: "task-views", route: "/tasks/", anchor: "task-views" },
  { part: 1, id: "task-card", route: "/tasks/", anchor: "task-card", gate: true },
  { part: 1, id: "task-drawer", route: "/tasks/", anchor: "task-drawer" },
  { part: 1, id: "project-row", route: "/projects/", anchor: "project-row", gate: true },
  { part: 1, id: "project-dash", route: "/projects/", anchor: "project-dash" },
  { part: 1, id: "final", final: true },
];

const PART_2_STEPS: TourStep[] = [
  { part: 2, id: "universe", route: "/universe/", anchor: "universe" },
  { part: 2, id: "capture", route: "/diario/", anchor: "capture" },
  { part: 2, id: "theme", route: "/diario/", anchor: "theme" },
  { part: 2, id: "locale", route: "/diario/", anchor: "locale" },
  { part: 2, id: "final", final: true },
];

export function tourSteps(part: TourPart, options: { hasApproveTarget?: boolean } = {}): TourStep[] {
  const steps = part === 1 ? PART_1_STEPS : PART_2_STEPS;
  if (part !== 1 || options.hasApproveTarget !== false) return steps;
  return steps.filter((step) => step.id !== "todo-approva");
}

export function initialTourState(part: TourPart): TourMachineState {
  return { part, index: 0, completed: false };
}

export function currentTourStep(
  state: TourMachineState,
  steps: readonly TourStep[] = tourSteps(state.part),
): TourStep | null {
  if (state.completed) return null;
  return steps[state.index] ?? null;
}

export function canAdvanceTourStep(step: TourStep | null, event: "next" | "gate" | "skip"): boolean {
  if (!step) return false;
  if (!step.gate) return event === "next" || event === "skip";
  return event === "gate" || event === "skip";
}

export function advanceTour(
  state: TourMachineState,
  event: "next" | "gate" | "skip",
  steps: readonly TourStep[] = tourSteps(state.part),
): TourMachineState {
  const step = currentTourStep(state, steps);
  if (!canAdvanceTourStep(step, event)) return state;
  if (state.index >= steps.length - 1 || step?.final) {
    return { ...state, completed: true };
  }
  return { ...state, index: state.index + 1 };
}

export function shouldSkipMissingAnchor(step: TourStep | null): boolean {
  return step?.id === "todo-approva";
}
