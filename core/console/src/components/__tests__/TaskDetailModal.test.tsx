import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { TaskResponse } from "@/lib/types";

// Mock API module
vi.mock("@/lib/api", () => ({
  getMe: vi.fn().mockResolvedValue({ username: "emilio" }),
  getComments: vi.fn().mockResolvedValue([]),
  createComment: vi.fn(),
  updateComment: vi.fn(),
  deleteComment: vi.fn(),
  addReaction: vi.fn(),
  removeReaction: vi.fn(),
  updateTask: vi.fn(),
  listUsers: vi.fn().mockResolvedValue([]),
  getTask: vi.fn(),
  getProjectRaci: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/config", () => ({
  API_BASE_URL: "https://api.test",
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    status: "authenticated",
    permissions: { canWrite: true, canAdmin: false, canView: true },
  }),
}));

vi.mock("../projects/PullRequestSection", () => ({
  default: () => <div data-testid="pr-section">PR Section</div>,
}));

vi.mock("../projects/TaskCostSection", () => ({
  default: () => <div data-testid="cost-section">Cost Section</div>,
}));

vi.mock("@/components/triage/ScoreInput", () => ({
  default: (props: Record<string, unknown>) => (
    <div data-testid="score-input">
      Score: I={String(props.impact)} C={String(props.confidence)} E={String(props.ease)}
    </div>
  ),
}));

import { getTask } from "@/lib/api";
import TaskDetailModal from "../projects/TaskDetailModal";

function makeMockTask(overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id: "task-123",
    title: "Add pricing page",
    description: null,
    status: "in_progress",
    priority: "high",
    tags: ["frontend", "design"],
    created_by: "marvis",
    owner_id: null,
    owner: null,
    project: "test-project",
    source: "console",
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
    pr_status: null,
    review_feedback: null,
    ...overrides,
  };
}

const mockTask = makeMockTask();

const defaultProps = {
  task: mockTask,
  slug: "test-project",
  onClose: vi.fn(),
  onStatusChange: vi.fn(),
  onTaskUpdated: vi.fn(),
  onTaskDeleted: vi.fn(),
};

describe("TaskDetailModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getTask).mockResolvedValue(mockTask);
  });

  it("renders task title", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByText("Add pricing page")).toBeInTheDocument();
  });

  it("renders status dropdown with current status", () => {
    render(<TaskDetailModal {...defaultProps} />);
    const statusSelect = screen.getByRole("combobox", { name: /task status/i });
    expect(statusSelect).toBeInTheDocument();
    expect(statusSelect).toHaveValue("in_progress");
  });

  it("renders priority", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  it("renders tags", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByText("frontend")).toBeInTheDocument();
    expect(screen.getByText("design")).toBeInTheDocument();
  });

  it("renders created_by", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByText("marvis")).toBeInTheDocument();
  });

  it("renders Edit button when user has write permission", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByRole("button", { name: /edit task/i })).toBeInTheDocument();
  });

  it("renders Delete button when user has write permission", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByRole("button", { name: /delete task/i })).toBeInTheDocument();
  });

  it("renders Close button", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByRole("button", { name: /close/i })).toBeInTheDocument();
  });

  it("calls onClose when close button clicked", async () => {
    const user = userEvent.setup();
    render(<TaskDetailModal {...defaultProps} />);
    await user.click(screen.getByRole("button", { name: /^close$/i }));
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it("calls onClose when backdrop clicked", async () => {
    const user = userEvent.setup();
    const { container } = render(<TaskDetailModal {...defaultProps} />);
    const backdrop = container.firstChild as HTMLElement;
    await user.click(backdrop);
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it("renders PullRequestSection", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByTestId("pr-section")).toBeInTheDocument();
  });

  it("renders TaskCostSection", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByTestId("cost-section")).toBeInTheDocument();
  });

  it("renders ScoreInput component", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.getByTestId("score-input")).toBeInTheDocument();
  });

  it("renders Comments section", async () => {
    render(<TaskDetailModal {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("Comments")).toBeInTheDocument();
    });
  });

  it("hides tags section when tags array is empty", () => {
    const noTagsTask = makeMockTask({ tags: [] });
    render(<TaskDetailModal {...defaultProps} task={noTagsTask} />);
    // Tags section should not render any tag chips
    expect(screen.queryByText("frontend")).not.toBeInTheDocument();
  });

  it("renders description with color coding when using template format", async () => {
    const descTask = makeMockTask({
      description: "Devo aggiungere la pagina pricing perch\u00e9 il cliente la richiede. Attenzione a dipendenze CSS.",
    });
    vi.mocked(getTask).mockResolvedValue(descTask);
    render(<TaskDetailModal {...defaultProps} task={descTask} />);
    await waitFor(() => {
      expect(screen.getByText("Description")).toBeInTheDocument();
    });
  });

  it("renders plain description when not matching template", async () => {
    const plainDesc = makeMockTask({ description: "Just a plain description text." });
    vi.mocked(getTask).mockResolvedValue(plainDesc);
    render(<TaskDetailModal {...defaultProps} task={plainDesc} />);
    await waitFor(() => {
      expect(screen.getByText("Just a plain description text.")).toBeInTheDocument();
    });
  });

  it("does not render description block when description is null", () => {
    const noDesc = makeMockTask({ description: null });
    render(<TaskDetailModal {...defaultProps} task={noDesc} />);
    // The "Description" label should not appear
    expect(screen.queryByText("Description")).not.toBeInTheDocument();
  });

  it("shows Approve button for pending tasks", () => {
    const pendingTask = makeMockTask({ status: "pending" });
    vi.mocked(getTask).mockResolvedValue(pendingTask);
    render(<TaskDetailModal {...defaultProps} task={pendingTask} />);
    expect(screen.getByRole("button", { name: /approve task/i })).toBeInTheDocument();
  });

  it("does not show Approve button for non-pending tasks", () => {
    render(<TaskDetailModal {...defaultProps} />);
    expect(screen.queryByRole("button", { name: /approve task/i })).not.toBeInTheDocument();
  });

  it("renders owner info when owner is set", () => {
    const ownedTask = makeMockTask({
      owner: { id: "u1", slug: "emilio", display_name: "Emilio", avatar_color: "#ff0000" },
      owner_id: "u1",
    });
    vi.mocked(getTask).mockResolvedValue(ownedTask);
    render(<TaskDetailModal {...defaultProps} task={ownedTask} />);
    expect(screen.getByText("Emilio")).toBeInTheDocument();
  });

  it("renders score values when task has ICE scores", () => {
    const scoredTask = makeMockTask({ impact: 8, confidence: 7, ease: 6 });
    vi.mocked(getTask).mockResolvedValue(scoredTask);
    render(<TaskDetailModal {...defaultProps} task={scoredTask} />);
    expect(screen.getByText(/I=8/)).toBeInTheDocument();
    expect(screen.getByText(/C=7/)).toBeInTheDocument();
    expect(screen.getByText(/E=6/)).toBeInTheDocument();
  });

  it("shows valid status transitions in dropdown", () => {
    // in_progress can transition to: completed, failed
    render(<TaskDetailModal {...defaultProps} />);
    const statusSelect = screen.getByRole("combobox", { name: /task status/i });
    const options = Array.from(statusSelect.querySelectorAll("option")).map((o) => o.value);
    expect(options).toContain("in_progress");
    expect(options).toContain("completed");
    expect(options).toContain("failed");
    expect(options).not.toContain("pending");
  });
});
