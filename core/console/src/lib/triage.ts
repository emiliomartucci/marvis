import { listTasks } from "./api";
import type { TaskResponse, TriageFilters } from "./types";

export const TRIAGE_ACTIVE_STATUSES = "pending,approved,in_progress,review";
export const TRIAGE_CLOSED_STATUSES = "completed,failed,rejected";
export const TRIAGE_ACTIVE_LIMIT = 500;
export const TRIAGE_CLOSED_LIMIT = 100;

type IgnoredFilterKey = keyof TriageFilters;

export function filterTriageTasks(
  tasks: TaskResponse[],
  filters: TriageFilters,
  ignoredFilters: IgnoredFilterKey[] = []
): TaskResponse[] {
  const ignored = new Set<IgnoredFilterKey>(ignoredFilters);

  return tasks.filter((task) => {
    if (!ignored.has("status") && filters.status.length > 0 && !filters.status.includes(task.status)) {
      return false;
    }
    if (!ignored.has("kind") && filters.kind.length > 0 && !filters.kind.includes(task.kind)) {
      return false;
    }
    if (!ignored.has("project") && filters.project.length > 0 && !filters.project.includes(task.project)) {
      return false;
    }
    if (!ignored.has("priority") && filters.priority.length > 0 && !filters.priority.includes(task.priority)) {
      return false;
    }
    if (!ignored.has("delegation") && filters.delegation.length > 0) {
      const taskDelegation = task.delegation || "unscored";
      if (!filters.delegation.includes(taskDelegation as TriageFilters["delegation"][number])) {
        return false;
      }
    }
    return true;
  });
}

export async function loadTriageTasks(
  opts?: { signal?: AbortSignal }
): Promise<TaskResponse[]> {
  const [activeTasks, closedTasks] = await Promise.all([
    listTasks(
      {
        status: TRIAGE_ACTIVE_STATUSES,
        sort: "ice_score:desc",
        limit: TRIAGE_ACTIVE_LIMIT,
      },
      opts
    ),
    listTasks(
      {
        status: TRIAGE_CLOSED_STATUSES,
        sort: "updated_at:desc",
        limit: TRIAGE_CLOSED_LIMIT,
      },
      opts
    ),
  ]);

  return [...activeTasks, ...closedTasks];
}
