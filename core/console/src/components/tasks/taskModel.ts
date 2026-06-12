import type {
  DelegationType,
  ProjectInfo,
  PullRequest,
  TaskResponse,
  TaskStatus,
} from "@/lib/types";

export type TaskLifecycleStatus = "approved" | "in_progress" | "review" | "completed" | "rejected";
export type TaskViewMode = "kanban" | "project" | "calendar";
export type TaskOwnerFilter = "all" | "agent" | "human";

export interface TaskColumnDefinition {
  status: TaskLifecycleStatus;
  collapsedByDefault: boolean;
}

export const TASK_LIFECYCLE_COLUMNS: readonly TaskColumnDefinition[] = [
  { status: "approved", collapsedByDefault: false },
  { status: "in_progress", collapsedByDefault: false },
  { status: "review", collapsedByDefault: false },
  { status: "completed", collapsedByDefault: true },
  { status: "rejected", collapsedByDefault: true },
];

export const TASK_LIFECYCLE_STATUSES = TASK_LIFECYCLE_COLUMNS.map((column) => column.status);

export type ParsedTaskDescription =
  | {
      kind: "structured";
      do: string;
      why: string;
      watch: string;
    }
  | {
      kind: "plain";
      text: string;
    };

export interface TaskActionDefinition {
  id: "start" | "send_review" | "complete" | "approve" | "return" | "reopen";
  nextStatus: TaskStatus;
  needsFeedback?: boolean;
  opensFollowUp?: boolean;
}

function trimSentenceEdge(value: string): string {
  let next = value.trim();
  while (next.endsWith(".") || next.endsWith(" ")) {
    next = next.slice(0, -1).trim();
  }
  return next;
}

function stripLeadingSeparator(value: string): string {
  let next = value.trim();
  while (next.startsWith(":") || next.startsWith("-")) {
    next = next.slice(1).trim();
  }
  return next;
}

export function parseTaskDescription(description: string | null | undefined): ParsedTaskDescription {
  const text = (description ?? "").trim();
  if (!text) return { kind: "plain", text: "" };

  const lower = text.toLocaleLowerCase("it");
  const watchNeedle = "attenzione a";
  const watchIndex = lower.indexOf(watchNeedle);
  if (watchIndex < 0) return { kind: "plain", text };

  const beforeWatch = trimSentenceEdge(text.slice(0, watchIndex));
  const lowerBeforeWatch = beforeWatch.toLocaleLowerCase("it");
  const whyNeedles = [" perche ", " perché "];
  const whyHit = whyNeedles
    .map((needle) => ({ needle, index: lowerBeforeWatch.indexOf(needle) }))
    .filter((hit) => hit.index >= 0)
    .sort((left, right) => left.index - right.index)[0];
  if (!whyHit) return { kind: "plain", text };

  const action = trimSentenceEdge(beforeWatch.slice(0, whyHit.index));
  const why = trimSentenceEdge(beforeWatch.slice(whyHit.index + whyHit.needle.length));
  const watchStart = watchIndex + watchNeedle.length;
  const watch = trimSentenceEdge(stripLeadingSeparator(text.slice(watchStart)));

  if (!action || !why || !watch) return { kind: "plain", text };
  return { kind: "structured", do: action, why, watch };
}

export function taskColumnTasks(tasks: TaskResponse[]): Record<TaskLifecycleStatus, TaskResponse[]> {
  return TASK_LIFECYCLE_STATUSES.reduce((columns, status) => {
    columns[status] = tasks.filter((task) => task.status === status);
    return columns;
  }, {} as Record<TaskLifecycleStatus, TaskResponse[]>);
}

export function taskActionDefinitions(task: Pick<TaskResponse, "status" | "pr_status">): TaskActionDefinition[] {
  if (task.status === "approved") {
    return [{ id: "start", nextStatus: "in_progress" }];
  }
  if (task.status === "in_progress") {
    return task.pr_status
      ? [{ id: "send_review", nextStatus: "review" }]
      : [{ id: "complete", nextStatus: "completed", opensFollowUp: true }];
  }
  if (task.status === "review") {
    return [
      { id: "approve", nextStatus: "completed", opensFollowUp: true },
      { id: "return", nextStatus: "in_progress", needsFeedback: true },
    ];
  }
  if (task.status === "completed" || task.status === "rejected") {
    return [{ id: "reopen", nextStatus: "in_progress" }];
  }
  return [];
}

export function isAgentOwned(task: Pick<TaskResponse, "delegation" | "owner_id">): boolean {
  return task.delegation === "agent" || task.delegation === "hybrid" || task.owner_id?.startsWith("agent:") === true;
}

export function matchesOwnerFilter(task: Pick<TaskResponse, "delegation" | "owner_id">, filter: TaskOwnerFilter): boolean {
  if (filter === "all") return true;
  const agentOwned = isAgentOwned(task);
  return filter === "agent" ? agentOwned : !agentOwned;
}

export function getTaskDueDate(task: Pick<TaskResponse, "due_date" | "created_at" | "updated_at">): string | null {
  return task.due_date ?? task.updated_at?.slice(0, 10) ?? task.created_at?.slice(0, 10) ?? null;
}

export function formatShortDate(value: string | null | undefined, locale: string): string {
  if (!value) return "n/a";
  const date = new Date(`${value.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "short" }).format(date);
}

export function formatMonthTitle(date: Date, locale: string): string {
  return new Intl.DateTimeFormat(locale, { month: "long", year: "numeric" }).format(date);
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

export function projectColor(project: ProjectInfo | undefined): string {
  const color = (project as (ProjectInfo & { color?: string | null }) | undefined)?.color?.trim();
  if (!color) return "var(--pir-border-strong)";
  if (color.startsWith("var(") || color.startsWith("hsl(")) return color;
  if (/^\d+(\.\d+)?\s+\d+(\.\d+)?%\s+\d+(\.\d+)?%$/u.test(color)) {
    return `hsl(${color})`;
  }
  return "var(--pir-border-strong)";
}

export function taskPrLabel(task: Pick<TaskResponse, "id" | "pr_status">, pr?: PullRequest | null): string | null {
  if (!task.pr_status) return null;
  const raw = pr?.id ?? task.id;
  return `#${raw.slice(-4)}`;
}

export function iceValue(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "-";
}

export function delegationGlyph(delegation: DelegationType | null | undefined): string {
  return delegation === "human" ? "person" : "agent";
}
