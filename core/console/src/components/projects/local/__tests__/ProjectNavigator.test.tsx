import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AnchorHTMLAttributes } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={typeof href === "string" ? href : String(href)} {...props}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/api", () => ({
  getPrograms: vi.fn(),
}));

import { getPrograms } from "@/lib/api";
import type { ProgramInfo, ProjectInfo } from "@/lib/types";
import ProjectNavigator from "../ProjectNavigator";

function project(overrides: Partial<ProjectInfo>): ProjectInfo {
  return {
    slug: "marvisx",
    name: "MarvisX",
    program: "marvis",
    language: "typescript",
    lifecycle: "active",
    phase: null,
    scope: "work",
    description: null,
    type: "code",
    repo_path: null,
    metadata_path: null,
    status: null,
    task_counts: { pending: 0, approved: 0, in_progress: 0, review: 0, completed: 0, rejected: 0, failed: 0 },
    last_handoff: null,
    last_status_update: null,
    on_server: true,
    color: null,
    ...overrides,
  };
}

const programs: ProgramInfo[] = [
  {
    name: "marvis",
    description: "",
    projects: [
      project({ slug: "brain", name: "MarvisX Brain", description: "memory layer" }),
      project({ slug: "hidden", name: "Hidden", on_server: false }),
    ],
  },
  {
    name: "personal",
    description: "",
    projects: [
      project({
        slug: "site",
        name: "Personal Site",
        program: "personal",
        scope: "personal",
        type: "work",
        language: null,
        description: "portfolio",
      }),
    ],
  },
];

describe("ProjectNavigator", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem("marvis:locale", "it");
    localStorage.removeItem("marvis:local-project-programs-collapsed");
    vi.mocked(getPrograms).mockResolvedValue(programs);
  });

  it("groups projects by program and filters search results", async () => {
    const user = userEvent.setup();
    render(<ProjectNavigator />);

    expect(await screen.findByText("marvis")).toBeInTheDocument();
    expect(screen.getByText("personal")).toBeInTheDocument();
    expect(screen.getByText("MarvisX Brain")).toBeInTheDocument();
    expect(screen.getByText("Personal Site")).toBeInTheDocument();
    expect(screen.queryByText("Hidden")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Cerca progetto..."), "site");

    await waitFor(() => {
      expect(screen.queryByText("MarvisX Brain")).not.toBeInTheDocument();
      expect(screen.getByText("Personal Site")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Comprimi programma: personal" })).not.toBeInTheDocument();
    });
  });
});
