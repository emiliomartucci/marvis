import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("../api", () => ({
  listTasks: vi.fn(),
}));

import { listTasks } from "../api";
import {
  filterTriageTasks,
  loadTriageTasks,
  TRIAGE_ACTIVE_LIMIT,
  TRIAGE_ACTIVE_STATUSES,
  TRIAGE_CLOSED_LIMIT,
  TRIAGE_CLOSED_STATUSES,
} from "../triage";
import type { TaskResponse, TriageFilters } from "../types";

describe("loadTriageTasks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads active tasks separately from recent closed tasks", async () => {
    vi.mocked(listTasks)
      .mockResolvedValueOnce([{ id: "active-task", status: "review" }] as never)
      .mockResolvedValueOnce([{ id: "closed-task", status: "completed" }] as never);

    const result = await loadTriageTasks();

    expect(listTasks).toHaveBeenNthCalledWith(
      1,
      {
        status: TRIAGE_ACTIVE_STATUSES,
        sort: "ice_score:desc",
        limit: TRIAGE_ACTIVE_LIMIT,
      },
      undefined
    );
    expect(listTasks).toHaveBeenNthCalledWith(
      2,
      {
        status: TRIAGE_CLOSED_STATUSES,
        sort: "updated_at:desc",
        limit: TRIAGE_CLOSED_LIMIT,
      },
      undefined
    );
    expect(result).toEqual([
      { id: "active-task", status: "review" },
      { id: "closed-task", status: "completed" },
    ]);
  });
});

describe("filterTriageTasks", () => {
  const tasks = [
    {
      id: "task-a",
      title: "A",
      description: null,
      kind: "normal",
      status: "approved",
      project: "marvisx",
      priority: "high",
      created_by: "tester",
      owner_id: null,
      owner: null,
      source: "manual",
      source_ref: null,
      tags: [],
      deleted_at: null,
      created_at: "2026-04-06T00:00:00Z",
      updated_at: "2026-04-06T00:00:00Z",
      impact: null,
      confidence: null,
      ease: null,
      delegation: "agent",
      ice_score: null,
      scored_by: null,
      scored_at: null,
      pr_status: null,
      review_feedback: null,
    },
    {
      id: "task-b",
      title: "B",
      description: null,
      kind: "idea",
      status: "in_progress",
      project: "marvisx",
      priority: "medium",
      created_by: "tester",
      owner_id: null,
      owner: null,
      source: "manual",
      source_ref: null,
      tags: [],
      deleted_at: null,
      created_at: "2026-04-06T00:00:00Z",
      updated_at: "2026-04-06T00:00:00Z",
      impact: null,
      confidence: null,
      ease: null,
      delegation: "human",
      ice_score: null,
      scored_by: null,
      scored_at: null,
      pr_status: null,
      review_feedback: null,
    },
    {
      id: "task-c",
      title: "C",
      description: null,
      kind: "normal",
      status: "approved",
      project: "acme",
      priority: "high",
      created_by: "tester",
      owner_id: null,
      owner: null,
      source: "manual",
      source_ref: null,
      tags: [],
      deleted_at: null,
      created_at: "2026-04-06T00:00:00Z",
      updated_at: "2026-04-06T00:00:00Z",
      impact: null,
      confidence: null,
      ease: null,
      delegation: null,
      ice_score: null,
      scored_by: null,
      scored_at: null,
      pr_status: null,
      review_feedback: null,
    },
  ] satisfies TaskResponse[];

  const filters: TriageFilters = {
    status: ["approved"],
    kind: [],
    project: ["marvisx"],
    priority: [],
    delegation: [],
  };

  it("applies all active filters by default", () => {
    expect(filterTriageTasks(tasks, filters).map((task) => task.id)).toEqual(["task-a"]);
  });

  it("can ignore status when computing faceted status counts", () => {
    expect(filterTriageTasks(tasks, filters, ["status"]).map((task) => task.id)).toEqual([
      "task-a",
      "task-b",
    ]);
  });

  it("can ignore project when computing faceted project counts", () => {
    expect(filterTriageTasks(tasks, filters, ["project"]).map((task) => task.id)).toEqual([
      "task-a",
      "task-c",
    ]);
  });

  it("filters ideas separately from normal tasks", () => {
    expect(filterTriageTasks(tasks, { ...filters, status: [], kind: ["idea"] }).map((task) => task.id)).toEqual([
      "task-b",
    ]);
  });
});
