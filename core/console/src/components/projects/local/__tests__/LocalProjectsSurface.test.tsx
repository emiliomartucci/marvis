import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
let routeSearchParams = new URLSearchParams("slug=marvisx");

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useSearchParams: () => routeSearchParams,
}));

vi.mock("@/lib/api", () => ({
  createTodoLocal: vi.fn(),
  deleteManualProjectEdge: vi.fn(),
  getProjectDocs: vi.fn(),
  getProjectFile: vi.fn(),
  getProjectGitBranches: vi.fn(),
  getProjectGitLog: vi.fn(),
  getProjectDetail: vi.fn(),
  getPrograms: vi.fn(),
  listBrainJournal: vi.fn(),
  listLearnings: vi.fn(),
  listTasks: vi.fn(),
  updateProjectColor: vi.fn(),
  upsertManualProjectEdge: vi.fn(),
}));

import {
  getProjectDocs,
  getProjectFile,
  getProjectGitBranches,
  getProjectGitLog,
  getProjectDetail,
  getPrograms,
  listBrainJournal,
  listLearnings,
  listTasks,
  updateProjectColor,
} from "@/lib/api";
import type { DocEntry, ProgramInfo, ProjectDetail, ProjectInfo } from "@/lib/types";
import LocalProjectsSurface from "../LocalProjectsSurface";

function counts() {
  return { pending: 0, approved: 0, in_progress: 0, review: 0, completed: 0, rejected: 0, failed: 0 };
}

function project(overrides: Partial<ProjectInfo> = {}): ProjectInfo {
  return {
    slug: "marvisx",
    name: "MarvisX",
    program: "marvis",
    language: "typescript",
    lifecycle: "active",
    phase: null,
    scope: "work",
    description: "Company brain",
    type: "work",
    repo_path: null,
    metadata_path: null,
    status: null,
    task_counts: counts(),
    last_handoff: null,
    last_status_update: null,
    on_server: true,
    color: null,
    ...overrides,
  };
}

function detail(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    slug: "marvisx",
    name: "MarvisX",
    program: "marvis",
    language: "typescript",
    lifecycle: "active",
    phase: null,
    scope: "work",
    description: "Company brain",
    type: "work",
    repo_path: null,
    metadata_path: null,
    context_md: null,
    config: {},
    deploy: null,
    color: null,
    handoffs: [],
    plans: [],
    solutions: [],
    kg_context: { neighbors: [] },
    ...overrides,
  };
}

const programs: ProgramInfo[] = [
  { name: "marvis", description: "", projects: [project()] },
];

function mockProjectLoad(docs: DocEntry[] = []) {
  vi.mocked(getPrograms).mockResolvedValue(programs);
  vi.mocked(getProjectDetail).mockResolvedValue(detail());
  vi.mocked(listTasks).mockResolvedValue([]);
  vi.mocked(listBrainJournal).mockResolvedValue({ items: [] });
  vi.mocked(getProjectDocs).mockResolvedValue(docs);
  vi.mocked(listLearnings).mockResolvedValue([]);
  vi.mocked(getProjectGitBranches).mockResolvedValue([]);
  vi.mocked(getProjectGitLog).mockResolvedValue([]);
}

describe("LocalProjectsSurface", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubEnv("NEXT_PUBLIC_LOCAL_MODE", "true");
    localStorage.setItem("marvis:locale", "it");
    routeSearchParams = new URLSearchParams("slug=marvisx");
    push.mockReset();
  });

  it("persists project color through the PATCH flow", async () => {
    const user = userEvent.setup();
    mockProjectLoad();
    vi.spyOn(window, "getComputedStyle").mockImplementation(() => ({
      getPropertyValue: (token: string) => token === "--pir-accent" ? "0 100% 50%" : "",
    } as CSSStyleDeclaration));
    vi.mocked(updateProjectColor).mockResolvedValue(detail({ color: "#ff0000" }));

    render(<LocalProjectsSurface />);

    await user.click(await screen.findByRole("button", { name: "Colore progetto" }));
    await user.click(screen.getByRole("button", { name: "Colore progetto 1" }));

    await waitFor(() => {
      expect(updateProjectColor).toHaveBeenCalledWith("marvisx", "#ff0000");
      expect(screen.getByText("Colore aggiornato")).toBeInTheDocument();
    });
  });

  it("opens markdown documents for inspection without editor controls", async () => {
    const user = userEvent.setup();
    const docs: DocEntry[] = [
      {
        filename: "docs/readme.md",
        title: "Readme",
        date: "2026-06-12",
        category: "docs",
      },
    ];
    mockProjectLoad(docs);
    vi.mocked(getProjectFile).mockResolvedValue({
      filename: "readme.md",
      path: "docs/readme.md",
      content: "# Original",
      size: 10,
    });

    render(<LocalProjectsSurface />);

    await user.click(await screen.findByRole("button", { name: /Readme/ }));
    await waitFor(() => {
      expect(getProjectFile).toHaveBeenCalledWith(
        "marvisx",
        "docs/readme.md",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
    });
    expect(screen.queryByRole("button", { name: "Modifica" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Salva" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});
