import { describe, expect, it } from "vitest";
import type { ProjectInfo, TaskResponse } from "@/lib/types";
import {
  TASK_LIFECYCLE_STATUSES,
  parseTaskDescription,
  projectColor,
  taskActionDefinitions,
  taskColumnTasks,
} from "../taskModel";

function task(id: string, status: TaskResponse["status"]): TaskResponse {
  return {
    id,
    title: id,
    description: null,
    kind: "normal",
    status,
    project: "marvisx",
    priority: "medium",
    created_by: "emilio",
    owner_id: null,
    owner: null,
    source: "console",
    source_ref: null,
    tags: [],
    deleted_at: null,
    created_at: "2026-06-12T09:00:00Z",
    updated_at: "2026-06-12T09:00:00Z",
    impact: null,
    confidence: null,
    ease: null,
    delegation: null,
    ice_score: null,
    scored_by: null,
    scored_at: null,
    pr_status: null,
    review_feedback: null,
    due_date: null,
    completion_mode: "pr",
  };
}

describe("taskModel", () => {
  it("maps the real task lifecycle statuses into kanban columns", () => {
    expect(TASK_LIFECYCLE_STATUSES).toEqual([
      "approved",
      "in_progress",
      "review",
      "completed",
      "rejected",
    ]);

    const columns = taskColumnTasks([
      task("todo", "approved"),
      task("doing", "in_progress"),
      task("needs-review", "review"),
      task("done", "completed"),
      task("discarded", "rejected"),
      task("legacy-pending", "pending"),
    ]);

    expect(columns.approved.map((item) => item.id)).toEqual(["todo"]);
    expect(columns.in_progress.map((item) => item.id)).toEqual(["doing"]);
    expect(columns.review.map((item) => item.id)).toEqual(["needs-review"]);
    expect(columns.completed.map((item) => item.id)).toEqual(["done"]);
    expect(columns.rejected.map((item) => item.id)).toEqual(["discarded"]);
  });

  it("parses do/why/watch from the live free-text convention", () => {
    expect(
      parseTaskDescription(
        "Devo costruire la board perche serve al gate umano. Attenzione a regressioni flag-off.",
      ),
    ).toEqual({
      kind: "structured",
      do: "Devo costruire la board",
      why: "serve al gate umano",
      watch: "regressioni flag-off",
    });
  });

  it("falls back to plain text when the convention is incomplete", () => {
    expect(parseTaskDescription("Solo testo libero")).toEqual({
      kind: "plain",
      text: "Solo testo libero",
    });
  });

  it("accepts persisted hex colors and falls back to a neutral token", () => {
    const project = (color: string | null): ProjectInfo =>
      ({ slug: "marvisx", color } as unknown as ProjectInfo);

    // project_gui_metadata (migration 152) persists colors as #rrggbb.
    expect(projectColor(project("#E2725B"))).toBe("#E2725B");
    expect(projectColor(project("18 95% 54%"))).toBe("hsl(18 95% 54%)");
    expect(projectColor(project("hsl(18 95% 54%)"))).toBe("hsl(18 95% 54%)");
    expect(projectColor(project(null))).toBe("var(--pir-border-strong)");
    expect(projectColor(project("not-a-color"))).toBe("var(--pir-border-strong)");
    expect(projectColor(undefined)).toBe("var(--pir-border-strong)");
  });

  it("derives per-state action definitions from status and PR presence", () => {
    expect(taskActionDefinitions({ status: "approved", pr_status: null }).map((action) => action.id)).toEqual(["start"]);
    expect(taskActionDefinitions({ status: "in_progress", pr_status: null }).map((action) => action.id)).toEqual(["complete"]);
    expect(taskActionDefinitions({ status: "in_progress", pr_status: "open" }).map((action) => action.id)).toEqual(["send_review"]);
    expect(taskActionDefinitions({ status: "review", pr_status: "open" }).map((action) => action.id)).toEqual(["approve", "return"]);
    expect(taskActionDefinitions({ status: "completed", pr_status: "merged" }).map((action) => action.id)).toEqual(["reopen"]);
  });
});
