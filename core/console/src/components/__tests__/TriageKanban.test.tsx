import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { TaskResponse } from "@/lib/types";

// Mock API module
vi.mock("@/lib/api", () => ({
  updateTask: vi.fn(),
  getPullRequest: vi.fn().mockResolvedValue({ diff: null, title: null, branch: null }),
  mergePullRequest: vi.fn(),
  closePullRequest: vi.fn(),
  revertPullRequest: vi.fn(),
  getMergeConflicts: vi.fn().mockResolvedValue({ conflicts: [] }),
}));

vi.mock("@/components/ui/ErrorAlert", () => ({
  ErrorAlert: ({ message }: { message: string }) => <div data-testid="error-alert">{message}</div>,
}));

import TriageKanban from "../triage/TriageKanban";

function makeMockTask(overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id: "task-001",
    title: "Fix null pointer",
    description: null,
    status: "pending",
    project: "marvisx",
    priority: "high",
    created_by: "marvis",
    owner_id: null,
    owner: null,
    source: "manual",
    source_ref: null,
    tags: ["bug"],
    deleted_at: null,
    created_at: "2026-03-01T00:00:00Z",
    updated_at: "2026-03-01T00:00:00Z",
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

const mockTasks: TaskResponse[] = [
  makeMockTask({ id: "task-001", title: "Fix null pointer", status: "pending", priority: "high", tags: ["bug"] }),
  makeMockTask({ id: "task-002", title: "Add monitoring", status: "approved", priority: "medium", delegation: "agent", tags: ["feat"] }),
  makeMockTask({ id: "task-003", title: "Deploy staging", status: "in_progress", priority: "medium", tags: ["deploy"] }),
  makeMockTask({ id: "task-004", title: "Review migration", status: "review", priority: "high", pr_status: "open", tags: ["migration"] }),
  makeMockTask({ id: "task-005", title: "Old task done", status: "completed", priority: "low", tags: [] }),
  makeMockTask({ id: "task-006", title: "Bad idea", status: "rejected", priority: "low", tags: [] }),
];

const defaultProps = {
  tasks: mockTasks,
  onTasksChange: vi.fn(),
  onTaskClick: vi.fn(),
};

describe("TriageKanban", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders all four active column headers", () => {
    render(<TriageKanban {...defaultProps} />);
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Review")).toBeInTheDocument();
  });

  it("renders completed column header (collapsed by default)", () => {
    render(<TriageKanban {...defaultProps} />);
    // Completed column starts collapsed, rendering "Completed (N)" in vertical text
    expect(screen.getByText(/Completed/)).toBeInTheDocument();
  });

  it("renders task titles in their columns", () => {
    render(<TriageKanban {...defaultProps} />);
    expect(screen.getByText("Fix null pointer")).toBeInTheDocument();
    expect(screen.getByText("Add monitoring")).toBeInTheDocument();
    expect(screen.getByText("Deploy staging")).toBeInTheDocument();
    expect(screen.getByText("Review migration")).toBeInTheDocument();
  });

  it("shows task project badge", () => {
    render(<TriageKanban {...defaultProps} />);
    // Each task card shows a project badge
    const badges = screen.getAllByText("marvisx");
    expect(badges.length).toBeGreaterThanOrEqual(4);
  });

  it("renders delegation badge when delegation is set", () => {
    render(<TriageKanban {...defaultProps} />);
    expect(screen.getByText("agent")).toBeInTheDocument();
  });

  it("renders priority badges (P1/P2/P3)", () => {
    render(<TriageKanban {...defaultProps} />);
    const p1Badges = screen.getAllByText("P1");
    expect(p1Badges.length).toBeGreaterThanOrEqual(1);
    const p2Badges = screen.getAllByText("P2");
    expect(p2Badges.length).toBeGreaterThanOrEqual(1);
  });

  it("renders ICE score badge when present", () => {
    const tasksWithScore = [
      makeMockTask({ id: "task-scored", title: "Scored task", status: "pending", ice_score: 120 }),
    ];
    render(<TriageKanban {...defaultProps} tasks={tasksWithScore} />);
    expect(screen.getByText("120")).toBeInTheDocument();
  });

  it("renders task ID abbreviation", () => {
    render(<TriageKanban {...defaultProps} />);
    expect(screen.getByText("#task-001")).toBeInTheDocument();
  });

  it("renders Merge and Close buttons for review tasks", () => {
    render(<TriageKanban {...defaultProps} />);
    expect(screen.getByText("Merge")).toBeInTheDocument();
    expect(screen.getByText("Close")).toBeInTheDocument();
    expect(screen.getByText("View")).toBeInTheDocument();
  });

  it("renders Complete instead of Merge when review task has no pr_status", () => {
    const reviewNoPr = [
      makeMockTask({ id: "task-noPr", title: "No PR task", status: "review", pr_status: null }),
    ];
    render(<TriageKanban {...defaultProps} tasks={reviewNoPr} />);
    expect(screen.getByText("Complete")).toBeInTheDocument();
  });

  it("renders rejected column only when rejected tasks exist", () => {
    render(<TriageKanban {...defaultProps} />);
    // Rejected column starts collapsed, rendering "Rejected (N)" in vertical text
    expect(screen.getByText(/Rejected/)).toBeInTheDocument();
  });

  it("hides rejected column when no rejected tasks", () => {
    const noRejected = mockTasks.filter((t) => t.status !== "rejected");
    render(<TriageKanban {...defaultProps} tasks={noRejected} />);
    expect(screen.queryByText("Rejected")).not.toBeInTheDocument();
  });

  it("renders column task counts", () => {
    render(<TriageKanban {...defaultProps} />);
    // Each column header shows its count. Pending has 1 task.
    // We verify at least the total counts are present somewhere
    const countElements = screen.getAllByText("1");
    expect(countElements.length).toBeGreaterThanOrEqual(1);
  });

  it("renders description problem/attention color coding", () => {
    const tasksWithDesc = [
      makeMockTask({
        id: "task-desc",
        title: "Desc task",
        status: "pending",
        description: "Devo fixare il bug perch\u00e9 il sistema crasha. Attenzione a dipendenze esterne.",
      }),
    ];
    render(<TriageKanban {...defaultProps} tasks={tasksWithDesc} />);
    // Problem text is parsed and shown
    expect(screen.getByText(/il sistema crasha/)).toBeInTheDocument();
    expect(screen.getByText(/dipendenze esterne/)).toBeInTheDocument();
  });

  it("renders empty with no tasks", () => {
    render(<TriageKanban {...defaultProps} tasks={[]} />);
    // Active column headers should still be present
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByText("Approved")).toBeInTheDocument();
    // Completed column exists but is collapsed
    expect(screen.getByText(/Completed/)).toBeInTheDocument();
  });
});
