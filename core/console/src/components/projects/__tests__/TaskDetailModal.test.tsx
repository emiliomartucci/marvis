import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the api module
vi.mock("@/lib/api", () => ({
  getComments: vi.fn().mockResolvedValue([]),
  createComment: vi.fn(),
  updateComment: vi.fn(),
  deleteComment: vi.fn(),
  addReaction: vi.fn(),
  removeReaction: vi.fn(),
  getMe: vi.fn().mockResolvedValue({ username: "emilio" }),
  getTask: vi.fn(),
  updateTask: vi.fn(),
  listUsers: vi.fn().mockResolvedValue([]),
  getProjectRaci: vi.fn().mockResolvedValue([]),
  getPullRequest: vi.fn().mockRejectedValue(new Error("404")),
  mergePullRequest: vi.fn(),
  closePullRequest: vi.fn(),
  approvePR: vi.fn(),
  requestPRChanges: vi.fn(),
  getTaskCostEntries: vi.fn().mockResolvedValue({
    task_id: "task-123",
    total_cost_usd: 0,
    total_bill_usd: 0,
    agent_cost_usd: 0,
    human_cost_usd: 0,
    billable_usd: 0,
    non_billable_usd: 0,
    entry_count: 0,
    entries: [],
    created_entry_id: null,
  }),
  createHumanCostEntry: vi.fn(),
}));

vi.mock("@/lib/config", () => ({
  API_BASE_URL: "https://api.test",
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    permissions: { canWrite: true },
  }),
}));

import TaskDetailModal from "../TaskDetailModal";
import { getTask, getPullRequest, getTaskCostEntries, getProjectRaci } from "@/lib/api";

const mockTask = {
  id: "task-123",
  title: "Add pricing page",
  description: "",
  status: "in_progress" as const,
  priority: "high" as const,
  tags: ["frontend", "design"],
  assigned_to: "emilio",
  created_by: "marvis",
  project: "test-project",
  source: "console" as const,
  source_ref: null,
  deleted_at: null,
  created_at: "2026-02-26T00:00:00Z",
  updated_at: "2026-02-26T00:00:00Z",
  impact: null,
  confidence: null,
  ease: null,
  delegation: null,
  ice_score: null,
  scored_by: null,
  scored_at: null,
};

const defaultProps = {
  task: mockTask,
  slug: "test-project",
  onClose: vi.fn(),
  onStatusChange: vi.fn(),
};

describe("TaskDetailModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getTask).mockResolvedValue(mockTask);
    vi.mocked(getPullRequest).mockRejectedValue(new Error("404"));
    vi.mocked(getTaskCostEntries).mockResolvedValue({
      task_id: "task-123",
      total_cost_usd: 0,
      total_bill_usd: 0,
      agent_cost_usd: 0,
      human_cost_usd: 0,
      billable_usd: 0,
      non_billable_usd: 0,
      entry_count: 0,
      entries: [],
      created_entry_id: null,
    });
    vi.mocked(getProjectRaci).mockResolvedValue([
      {
        id: "raci-1",
        project: "test-project",
        role: "responsible",
        user: {
          id: "usr-emilio",
          slug: "emilio",
          display_name: "emilio",
          avatar_color: "#6366f1",
        },
      },
    ]);
  });

  it("renders task title and details", async () => {
    render(<TaskDetailModal {...defaultProps} />);

    expect(screen.getByText("Add pricing page")).toBeInTheDocument();
    expect(screen.getByText("in_progress")).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  it("renders tags", () => {
    render(<TaskDetailModal {...defaultProps} />);

    expect(screen.getByText("frontend")).toBeInTheDocument();
    expect(screen.getByText("design")).toBeInTheDocument();
  });

  it("renders CommentThread component", async () => {
    render(<TaskDetailModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText("Comments")).toBeInTheDocument();
    });
  });

  it("calls onClose when close button clicked", async () => {
    const user = userEvent.setup();
    render(<TaskDetailModal {...defaultProps} />);

    await user.click(screen.getByRole("button", { name: /close/i }));
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it("calls onClose when backdrop clicked", async () => {
    const user = userEvent.setup();
    const { container } = render(<TaskDetailModal {...defaultProps} />);

    const backdrop = container.firstChild as HTMLElement;
    await user.click(backdrop);
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it("shows responsible user from project RACI", async () => {
    render(<TaskDetailModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("emilio")).toBeInTheDocument();
    });
  });

  it("renders Edit button", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByRole("button", { name: /edit task/i })).toBeInTheDocument();
  });

  it("renders Delete button", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByRole("button", { name: /delete task/i })).toBeInTheDocument();
  });

  it("renders status as a dropdown (combobox)", () => {
    render(<TaskDetailModal {...defaultProps} />);
    const statusSelect = screen.getByRole("combobox", { name: /task status/i });
    expect(statusSelect).toBeInTheDocument();
    expect(statusSelect).toHaveValue("in_progress");
  });
});
