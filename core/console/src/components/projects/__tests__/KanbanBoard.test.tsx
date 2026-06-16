import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const mockTasks = [
  { id: "task-001", title: "Fix null clear", description: "", status: "pending", priority: "high", tags: ["bug"], assigned_to: null, created_by: "marvis" },
  { id: "task-002", title: "Add monitoring", description: "", status: "approved", priority: "medium", tags: ["feat"], assigned_to: "agent:rx", created_by: "marvis" },
  { id: "task-003", title: "Deploy sprint", description: "", status: "completed", priority: "medium", tags: ["deploy"], assigned_to: null, created_by: "marvis" },
];

const mockFetch = vi.fn();
global.fetch = mockFetch;

vi.mock("@/lib/config", () => ({
  API_BASE_URL: "https://api.test",
}));

vi.mock("@/lib/api", () => ({
  getMe: vi.fn().mockResolvedValue({ username: "emilio" }),
}));

describe("KanbanBoard", () => {
  beforeEach(() => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockTasks),
    });
  });

  it("renders task cards in columns", async () => {
    const { default: KanbanBoard } = await import("../KanbanBoard");
    render(<KanbanBoard slug="test" />);
    await waitFor(() => {
      expect(screen.getByText("Fix null clear")).toBeInTheDocument();
      expect(screen.getByText("Add monitoring")).toBeInTheDocument();
    });
  });

  it("renders New Task button", async () => {
    const { default: KanbanBoard } = await import("../KanbanBoard");
    render(<KanbanBoard slug="test" />);
    await waitFor(() => {
      expect(screen.getByText("New Task")).toBeInTheDocument();
    });
  });

  it("renders Approve button on pending tasks", async () => {
    const { default: KanbanBoard } = await import("../KanbanBoard");
    render(<KanbanBoard slug="test" />);
    await waitFor(() => {
      const approveButtons = screen.getAllByText("Approve");
      expect(approveButtons.length).toBeGreaterThan(0);
    });
  });

  it("renders view toggle (Board/List)", async () => {
    const { default: KanbanBoard } = await import("../KanbanBoard");
    render(<KanbanBoard slug="test" />);
    await waitFor(() => {
      expect(screen.getByText("Board")).toBeInTheDocument();
      expect(screen.getByText("List")).toBeInTheDocument();
    });
  });
});
