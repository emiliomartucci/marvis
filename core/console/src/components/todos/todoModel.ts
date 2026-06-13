import type { TodoResponseLocal } from "@/lib/api";

export type TodoFilter = "all" | "decisions" | "promemoria" | "idea" | "brain";
export type TodoHorizon = "overdue" | "today" | "tomorrow" | "week" | "later";
export type TodoActionId =
  | "complete"
  | "discard"
  | "postpone"
  | "delegate"
  | "promote"
  | "confirm"
  | "revise"
  | "approve"
  | "reject"
  | "review"
  | "feedback";

export interface TodoActionDefinition {
  id: TodoActionId;
  tone: "primary" | "secondary" | "danger";
  needsFeedback?: boolean;
}

export interface TodoHorizonGroup {
  horizon: TodoHorizon;
  items: TodoResponseLocal[];
}

const HORIZON_ORDER: TodoHorizon[] = ["overdue", "today", "tomorrow", "week", "later"];
const DECISION_TYPES = new Set(["decidi", "approva", "rivedi"]);

function parseIsoDay(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return null;
  const [, year, month, day] = match;
  const date = new Date(Number(year), Number(month) - 1, Number(day));
  date.setHours(0, 0, 0, 0);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function isoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

export function todoHorizon(fu: string, baseDate = new Date()): TodoHorizon {
  const due = parseIsoDay(fu);
  if (!due) return "later";
  const base = new Date(baseDate);
  base.setHours(0, 0, 0, 0);
  const tomorrow = addDays(base, 1);
  const weekEnd = addDays(base, 7);

  if (due < base) return "overdue";
  if (isoDate(due) === isoDate(base)) return "today";
  if (isoDate(due) === isoDate(tomorrow)) return "tomorrow";
  if (due <= weekEnd) return "week";
  return "later";
}

export function groupTodosByHorizon(
  todos: TodoResponseLocal[],
  baseDate = new Date()
): TodoHorizonGroup[] {
  const groups = new Map<TodoHorizon, TodoResponseLocal[]>(
    HORIZON_ORDER.map((horizon) => [horizon, []])
  );

  for (const todo of todos) {
    groups.get(todoHorizon(todo.fu, baseDate))?.push(todo);
  }

  return HORIZON_ORDER.map((horizon) => ({
    horizon,
    items: (groups.get(horizon) ?? []).sort((left, right) => {
      const dateSort = left.fu.localeCompare(right.fu);
      if (dateSort !== 0) return dateSort;
      return right.updated_at.localeCompare(left.updated_at);
    }),
  }));
}

export function matchesTodoFilter(todo: TodoResponseLocal, filter: TodoFilter): boolean {
  if (filter === "all") return true;
  if (filter === "decisions") return DECISION_TYPES.has(todo.type);
  if (filter === "promemoria") return todo.type === "promemoria";
  if (filter === "idea") return todo.type === "idea";
  return todo.source === "brain" || todo.family === "system" || todo.origin?.kind === "finding" || todo.origin?.kind === "memory_op";
}

export function openTodos(todos: TodoResponseLocal[]): TodoResponseLocal[] {
  return todos.filter((todo) => todo.status === "aperto" || todo.status === "in_revisione");
}

export function isDelegable(todo: Pick<TodoResponseLocal, "doer">): boolean {
  return todo.doer !== "human";
}

export function doerGlyph(todo: Pick<TodoResponseLocal, "doer">): string {
  return todo.doer === "human" ? "👤" : "⚡";
}

export type TodoRowControl = "checkbox" | "gate";

/**
 * Left-side control of a list row (design mock `views.js` TodoRow):
 * - "checkbox" toggles completion directly — only where the type state machine
 *   allows `fatto` from `aperto` (promemoria, azione);
 * - "gate" is a colored status dot that opens the detail drawer (decidi,
 *   approva, rivedi, virtual items and idea, which resolves via Promote/Discard).
 */
export function todoRowControl(todo: Pick<TodoResponseLocal, "type" | "virtual">): TodoRowControl {
  if (todo.virtual) return "gate";
  return todo.type === "promemoria" || todo.type === "azione" ? "checkbox" : "gate";
}

/**
 * Row actions are a subset of the drawer actions (design mock `views.js`
 * rowActions): "complete" lives in the checkbox, and "discard" is exposed
 * on-row only for ideas.
 */
export function todoRowActionDefinitions(todo: TodoResponseLocal): TodoActionDefinition[] {
  return todoActionDefinitions(todo).filter((action) => {
    if (action.id === "complete") return false;
    if (action.id === "discard") return todo.type === "idea";
    return true;
  });
}

export function nextHorizonDate(fu: string, baseDate = new Date()): string {
  const due = parseIsoDay(fu) ?? baseDate;
  const base = new Date(baseDate);
  base.setHours(0, 0, 0, 0);
  const horizon = todoHorizon(fu, base);
  if (horizon === "overdue" || horizon === "today") return isoDate(addDays(base, 1));
  if (horizon === "tomorrow") return isoDate(addDays(base, 7));
  if (horizon === "week") return isoDate(addDays(due, 7));
  return isoDate(addDays(due, 14));
}

export function todoActionDefinitions(todo: TodoResponseLocal): TodoActionDefinition[] {
  if (todo.virtual || todo.type === "approva") {
    return [
      { id: "approve", tone: "primary" },
      { id: "reject", tone: "danger", needsFeedback: true },
    ];
  }

  if (todo.type === "promemoria") {
    return [
      { id: "complete", tone: "primary" },
      { id: "postpone", tone: "secondary" },
      { id: "discard", tone: "danger" },
    ];
  }

  if (todo.type === "azione") {
    return [
      { id: "complete", tone: "primary" },
      ...(isDelegable(todo) ? [{ id: "delegate", tone: "primary" } as const] : []),
      { id: "postpone", tone: "secondary" },
      { id: "discard", tone: "danger" },
    ];
  }

  if (todo.type === "idea") {
    return [
      { id: "promote", tone: "primary" },
      { id: "postpone", tone: "secondary" },
      { id: "discard", tone: "danger" },
    ];
  }

  if (todo.type === "decidi") {
    return [
      { id: "confirm", tone: "primary" },
      { id: "revise", tone: "secondary", needsFeedback: true },
    ];
  }

  return [
    { id: "review", tone: "primary" },
    ...(isDelegable(todo) ? [{ id: "delegate", tone: "primary" } as const] : []),
    { id: "feedback", tone: "secondary", needsFeedback: true },
  ];
}
