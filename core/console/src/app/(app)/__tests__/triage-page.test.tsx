import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { loadTriageTasks } = vi.hoisted(() => ({
  loadTriageTasks: vi.fn(),
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
  default: ({ tasks }: { tasks: Array<{ title: string }> }) => (
    <div data-testid="triage-kanban">{tasks.map((task) => task.title).join(",")}</div>
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
});
