import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/lib/api", () => ({
  getPrograms: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("slug=marvisx"),
  useRouter: () => ({ push: vi.fn() }),
}));

// These cases describe the v1 sidebar (search placeholder, active-row marker,
// active/total badge). The design flag defaults to v2, so without this mock they
// rendered v2 and asserted against markup that is not there — four failures the
// tree carried because no CI job ran this suite.
vi.mock("@/lib/useDesignV2", () => ({
  useDesignV2: () => false,
}));

import { getPrograms } from "@/lib/api";
import ProjectsSidebar from "../ProjectsSidebar";

const mockPrograms = [
  {
    name: "marvis",
    description: "Marvis platform",
    projects: [
      {
        slug: "marvisx",
        name: "MarvisX",
        program: "marvis",
        language: "python",
        lifecycle: null,
        phase: null,
        scope: null,
        description: null,
        type: null,
        repo_path: null,
        metadata_path: null,
        status: "active" as const,
        task_counts: { pending: 2, approved: 1, in_progress: 1, review: 0, completed: 8, rejected: 0, failed: 0 },
        last_handoff: "2026-02-25",
        last_status_update: null,
        on_server: true,
        path: "/var/marvisx/marvis/projects-personal/MarvisX",
      },
    ],
  },
  {
    name: "personal",
    description: "Personal projects",
    projects: [
      {
        slug: "emilio",
        name: "emilio",
        program: "personal",
        language: null,
        lifecycle: null,
        phase: null,
        scope: null,
        description: null,
        type: null,
        repo_path: null,
        metadata_path: null,
        status: "active" as const,
        task_counts: { pending: 0, approved: 0, in_progress: 0, review: 0, completed: 0, rejected: 0, failed: 0 },
        last_handoff: null,
        last_status_update: null,
        on_server: true,
        path: null,
      },
    ],
  },
];

describe("ProjectsSidebar", () => {
  beforeEach(() => {
    vi.mocked(getPrograms).mockResolvedValue(mockPrograms);
  });

  it("renders programs and projects", async () => {
    render(<ProjectsSidebar />);
    await waitFor(() => {
      // Program name is "marvis" (uppercase is CSS only)
      expect(screen.getByText("marvis")).toBeInTheDocument();
      expect(screen.getByText("marvisx")).toBeInTheDocument();
    });
  });

  it("renders search input", async () => {
    render(<ProjectsSidebar />);
    await waitFor(() => {
      expect(screen.getByPlaceholderText("Search projects...")).toBeInTheDocument();
    });
  });

  it("shows task counts for projects with tasks", async () => {
    render(<ProjectsSidebar />);
    await waitFor(() => {
      // active tasks = pending(2) + approved(1) + in_progress(1) = 4, total = 4 + 8 = 12
      expect(screen.getByText("4/12")).toBeInTheDocument();
    });
  });

  it("filters projects by search", async () => {
    const user = userEvent.setup();
    render(<ProjectsSidebar />);
    await waitFor(() => screen.getByText("marvisx"));

    await user.type(screen.getByPlaceholderText("Search projects..."), "emilio");
    expect(screen.queryByText("marvisx")).not.toBeInTheDocument();
    expect(screen.getByText("emilio")).toBeInTheDocument();
  });

  it("highlights active project", async () => {
    render(<ProjectsSidebar />);
    await waitFor(() => {
      const activeRow = screen.getByText("marvisx").closest("[data-active]");
      expect(activeRow?.getAttribute("data-active")).toBe("true");
    });
  });
});
