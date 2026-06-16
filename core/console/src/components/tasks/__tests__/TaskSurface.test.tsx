import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { TaskResponse } from "@/lib/types";
import { it as itDict } from "@/lib/i18n/it";
import { TaskActionBar, TaskDetailDrawer } from "../TaskSurface";

function makeTask(overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id: "task-1",
    title: "Implement task surface",
    description: "Devo implementare la surface perche serve alla GUI. Attenzione a test behavior.",
    kind: "normal",
    status: "approved",
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
    updated_at: "2026-06-12T10:00:00Z",
    impact: 8,
    confidence: 7,
    ease: 6,
    delegation: "agent",
    ice_score: 336,
    scored_by: "emilio",
    scored_at: "2026-06-12T09:00:00Z",
    pr_status: null,
    review_feedback: null,
    due_date: "2026-06-14",
    completion_mode: "pr",
    comments: [],
    ...overrides,
  };
}

describe("TaskSurface components", () => {
  it("renders the per-state action bar labels", () => {
    const onAction = vi.fn();
    const { rerender } = render(
      <TaskActionBar task={makeTask({ status: "approved" })} onAction={onAction} t={itDict.taskSurface} />,
    );
    expect(screen.getByRole("button", { name: "Avvia" })).toBeInTheDocument();

    rerender(<TaskActionBar task={makeTask({ status: "in_progress", pr_status: "open" })} onAction={onAction} t={itDict.taskSurface} />);
    expect(screen.getByRole("button", { name: "Manda in revisione" })).toBeInTheDocument();

    rerender(<TaskActionBar task={makeTask({ status: "review", pr_status: "open" })} onAction={onAction} t={itDict.taskSurface} />);
    expect(screen.getByRole("button", { name: "Approva" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rimanda" })).toBeInTheDocument();

    rerender(<TaskActionBar task={makeTask({ status: "completed", pr_status: "merged" })} onAction={onAction} t={itDict.taskSurface} />);
    expect(screen.getByRole("button", { name: "Riapri" })).toBeInTheDocument();
  });

  it("keeps postpone confirm disabled until a new date is selected", async () => {
    const user = userEvent.setup();
    const onPostpone = vi.fn();

    render(
      <TaskDetailDrawer
        task={makeTask()}
        open
        project={undefined}
        pr={null}
        comments={[]}
        onClose={vi.fn()}
        onPostpone={onPostpone}
        onAddNote={vi.fn()}
        onAction={vi.fn()}
        t={itDict.taskSurface}
        locale="it"
      />,
    );

    await user.click(screen.getByRole("button", { name: "Posticipa" }));
    const confirm = screen.getByRole("button", { name: "Conferma" });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByLabelText("Nuova data"), "2026-06-20");
    expect(confirm).not.toBeDisabled();

    await user.click(confirm);
    expect(onPostpone).toHaveBeenCalledWith(expect.objectContaining({ id: "task-1" }), "2026-06-20");
  });
});
