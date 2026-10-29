import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { loadTriageTasks, getEffectiveProjectAccess } = vi.hoisted(() => ({
  loadTriageTasks: vi.fn(),
  getEffectiveProjectAccess: vi.fn(),
}));

vi.mock("@/lib/api", async () => ({
  ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"),
  getEffectiveProjectAccess,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => ({ get: () => null }),
}));

vi.mock("@/lib/triage", async () => {
  const actual = await vi.importActual<typeof import("@/lib/triage")>("@/lib/triage");
  return {
    ...actual,
    loadTriageTasks,
  };
});

vi.mock("@/components/triage/TriageSidebar", () => ({
  default: () => <div data-testid="triage-sidebar" />,
}));

vi.mock("@/components/triage/TriageKanban", () => ({
  default: ({ tasks, canEditTask }: { tasks: Array<{ title: string; project: string }>; canEditTask: (task: { project: string }) => boolean }) => (
    <div data-testid="triage-kanban">{tasks.map((task) => `${task.title}:${canEditTask(task) ? "edit" : "view"}`).join(",")}</div>
  ),
}));

vi.mock("@/components/projects/TaskDetailModal", () => ({
  default: () => null,
}));

import TriagePage from "../triage/page";

describe("TriagePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => vi.unstubAllEnvs());

  it("refreshes tasks when the tab becomes visible again", async () => {
    loadTriageTasks
      .mockResolvedValueOnce([{ id: "task-1", title: "Pending A", status: "pending" }])
      .mockResolvedValueOnce([{ id: "task-1", title: "Working A", status: "in_progress" }]);

    render(<TriagePage />);

    await waitFor(() => expect(screen.getByTestId("triage-kanban").textContent).toContain("Pending A"));

    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => false,
    });

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    await waitFor(() => expect(screen.getByTestId("triage-kanban").textContent).toContain("Working A"));
    expect(loadTriageTasks).toHaveBeenCalledTimes(2);
  });

  it("keeps view-only Enterprise projects read-only in the task board", async () => {
    vi.stubEnv("NEXT_PUBLIC_ENTERPRISE_MODE", "1");
    loadTriageTasks.mockResolvedValue([
      { id: "task-a", title: "Pending A", status: "pending", project: "alpha" },
      { id: "task-b", title: "Pending B", status: "pending", project: "beta" },
    ]);
    getEffectiveProjectAccess.mockImplementation(async (project: string) => ({
      project, can_view: true, can_edit: project === "alpha", sources: [],
    }));

    render(<TriagePage />);

    await waitFor(() => expect(screen.getByTestId("triage-kanban").textContent).toContain("Pending A:edit"));
    expect(screen.getByTestId("triage-kanban").textContent).toContain("Pending B:view");
    expect(getEffectiveProjectAccess).toHaveBeenCalledWith("alpha");
    expect(getEffectiveProjectAccess).toHaveBeenCalledWith("beta");
  });
});
