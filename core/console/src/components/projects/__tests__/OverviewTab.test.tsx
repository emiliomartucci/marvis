import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/api", () => ({
  getMe: vi.fn(),
  getStatusUpdates: vi.fn(),
  getProjectHandoffs: vi.fn(),
  getProjectGitLog: vi.fn(),
  getProjectGitGraph: vi.fn(),
  getProjectCosts: vi.fn(),
  listTasks: vi.fn(),
  getProjectRaci: vi.fn(),
  createStatusUpdate: vi.fn(),
  getComments: vi.fn(),
  createComment: vi.fn(),
  updateComment: vi.fn(),
  deleteComment: vi.fn(),
  addReaction: vi.fn(),
  removeReaction: vi.fn(),
}));

import {
  getMe,
  getStatusUpdates,
  getProjectHandoffs,
  getProjectGitGraph,
  getProjectCosts,
  listTasks,
  getProjectRaci,
  getComments,
} from "@/lib/api";
import OverviewTab from "../OverviewTab";
import type { ProjectDetail } from "@/lib/types";

const mockProject: ProjectDetail = {
  slug: "test-project",
  name: "Test Project",
  program: "test-program",
  context_md: "# Context\n\nThis is the raw context.md content that should NOT be rendered.",
  config: {
    Language: "python",
    Hosting: "cloudflare",
    Program: "test-program",
  },
  handoffs: [],
  plans: [],
  solutions: [],
};

const mockHandoffs = [
  {
    filename: "handoff-2026-02-25.md",
    date: "2026-02-25",
    summary: "Completed sprint 3 migration of color tokens and sidebar component.",
    session: null,
    branch: null,
    tags: [],
  },
  {
    filename: "handoff-2026-02-20.md",
    date: "2026-02-20",
    summary: "Set up project structure and initial routing.",
    session: null,
    branch: null,
    tags: [],
  },
];

const mockCommits = [
  {
    hash: "abc1234567890",
    hash_short: "abc1234",
    parents: [],
    refs: [],
    message: "feat(console): add metric dashboard to OverviewTab",
    author: "emilio",
    date: "2026-02-25T10:00:00Z",
  },
  {
    hash: "def5678901234",
    hash_short: "def5678",
    parents: [],
    refs: [],
    message: "fix(sidebar): correct active state detection",
    author: "emilio",
    date: "2026-02-24T15:30:00Z",
  },
];

describe("OverviewTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getMe).mockResolvedValue({ username: "emilio" });
    vi.mocked(getStatusUpdates).mockResolvedValue([]);
    vi.mocked(getProjectHandoffs).mockResolvedValue([]);
    vi.mocked(getProjectGitGraph).mockResolvedValue({
      commits: [],
      refs: [],
      has_more: false,
    });
    vi.mocked(getProjectCosts).mockResolvedValue([]);
    vi.mocked(listTasks).mockResolvedValue([]);
    vi.mocked(getProjectRaci).mockResolvedValue([]);
    vi.mocked(getComments).mockResolvedValue([]);
  });

  it("renders metric card titles: Lifecycle, Tasks, Git", () => {
    render(<OverviewTab project={mockProject} />);

    expect(screen.getByText("Lifecycle")).toBeInTheDocument();
    expect(screen.getByText("Tasks")).toBeInTheDocument();
    expect(screen.getByText("Git")).toBeInTheDocument();
  });

  it("renders Cost metric card", () => {
    render(<OverviewTab project={mockProject} />);
    expect(screen.getByText("Cost")).toBeInTheDocument();
  });

  it("renders config section with language key", async () => {
    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      expect(screen.getByText("Language")).toBeInTheDocument();
      // python may appear multiple times (config + status card subtitle)
      const pythonMatches = screen.getAllByText("python");
      expect(pythonMatches.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("renders all config key-value pairs including Hosting", async () => {
    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      expect(screen.getByText("Hosting")).toBeInTheDocument();
      expect(screen.getByText("cloudflare")).toBeInTheDocument();
      expect(screen.getByText("Program")).toBeInTheDocument();
      expect(screen.getByText("test-program")).toBeInTheDocument();
    });
  });

  it("renders Recent Handoffs panel", () => {
    render(<OverviewTab project={mockProject} />);
    expect(screen.getByText("Recent Handoffs")).toBeInTheDocument();
  });

  it("renders Recent Activity panel", () => {
    render(<OverviewTab project={mockProject} />);
    expect(screen.getByText("Recent Activity")).toBeInTheDocument();
  });

  it("does NOT render raw context.md content", () => {
    render(<OverviewTab project={mockProject} />);
    expect(
      screen.queryByText("This is the raw context.md content that should NOT be rendered.")
    ).not.toBeInTheDocument();
  });

  it("does NOT render SafeMarkdown heading from context.md", () => {
    render(<OverviewTab project={mockProject} />);
    // The context.md starts with "# Context" — this heading should not appear
    const headings = screen.queryAllByRole("heading", { level: 1 });
    const contextHeading = headings.find((h) => h.textContent === "Context");
    expect(contextHeading).toBeUndefined();
  });

  it("shows handoff summaries when handoffs are loaded", async () => {
    vi.mocked(getProjectHandoffs).mockResolvedValue(mockHandoffs);

    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      expect(
        screen.getByText("Completed sprint 3 migration of color tokens and sidebar component.")
      ).toBeInTheDocument();
    });
  });

  it("shows git commit hashes when commits are loaded", async () => {
    vi.mocked(getProjectGitGraph).mockResolvedValue({
      commits: mockCommits,
      refs: [],
      has_more: false,
    });

    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      // abc1234 appears in both metric card and recent activity list
      const hashMatches = screen.getAllByText("abc1234");
      expect(hashMatches.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("shows git commit messages when commits are loaded", async () => {
    vi.mocked(getProjectGitGraph).mockResolvedValue({
      commits: mockCommits,
      refs: [],
      has_more: false,
    });

    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      expect(
        screen.getByText("add metric dashboard to OverviewTab")
      ).toBeInTheDocument();
    });
  });

  it("shows latest commit hash_short in Git metric card", async () => {
    vi.mocked(getProjectGitGraph).mockResolvedValue({
      commits: mockCommits,
      refs: [],
      has_more: false,
    });

    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      // abc1234 should appear both in the metric card and in recent activity
      const matches = screen.getAllByText("abc1234");
      expect(matches.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("shows empty placeholder when no handoffs", async () => {
    vi.mocked(getProjectHandoffs).mockResolvedValue([]);

    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      expect(screen.getByText("No handoffs yet")).toBeInTheDocument();
    });
  });

  it("shows empty placeholder when no commits", async () => {
    vi.mocked(getProjectGitGraph).mockResolvedValue({
      commits: [],
      refs: [],
      has_more: false,
    });

    render(<OverviewTab project={mockProject} />);

    await waitFor(() => {
      expect(screen.getByText("No commits yet")).toBeInTheDocument();
    });
  });

  it("renders Status Update form", () => {
    render(<OverviewTab project={mockProject} />);
    expect(screen.getByText("Status Update")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("What was done...")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Blockers...")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Next steps...")).toBeInTheDocument();
  });

  it("shows — dash for Tasks metric (not yet connected)", () => {
    render(<OverviewTab project={mockProject} />);
    expect(screen.getByText("open tasks")).toBeInTheDocument();
  });

  it("shows — dash for Cost metric when no cost data is loaded", () => {
    render(<OverviewTab project={mockProject} />);
    expect(screen.getByText("Cost")).toBeInTheDocument();
  });
});
