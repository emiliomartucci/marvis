import { cleanup, render, waitFor } from "@testing-library/react";
// @ts-expect-error jest-axe does not ship declarations in this package version.
import { axe } from "jest-axe";
import type { AnchorHTMLAttributes } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace }),
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
  APIError: class APIError extends Error {
    status: number;
    detail: unknown;

    constructor(message: string, status: number, detail?: unknown) {
      super(message);
      this.name = "APIError";
      this.status = status;
      this.detail = detail;
    }
  },
  applyVirtualTodoActionLocal: vi.fn(),
  createComment: vi.fn(),
  createTask: vi.fn(),
  createTodoLocal: vi.fn(),
  delegateTodoLocal: vi.fn(),
  getPrograms: vi.fn(),
  getPullRequest: vi.fn(),
  listBrainJournal: vi.fn(),
  listBrainRuns: vi.fn(),
  listTasks: vi.fn(),
  listTodosLocal: vi.fn(),
  mergePullRequest: vi.fn(),
  requestPRChanges: vi.fn(),
  triggerBrainRun: vi.fn(),
  updateTask: vi.fn(),
  updateTodoLocal: vi.fn(),
}));

import {
  getPrograms,
  listBrainJournal,
  listBrainRuns,
  listTasks,
  listTodosLocal,
} from "@/lib/api";
import DiarioPage from "../diario/page";
import TasksPage from "../tasks/page";
import TodosPage from "../todos/page";

describe("local surfaces a11y", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubEnv("NEXT_PUBLIC_LOCAL_MODE", "true");
    localStorage.setItem("marvis:locale", "it");
    vi.mocked(getPrograms).mockResolvedValue([]);
    vi.mocked(listBrainJournal).mockResolvedValue({ items: [] });
    vi.mocked(listBrainRuns).mockResolvedValue({ items: [] });
    vi.mocked(listTasks).mockResolvedValue([]);
    vi.mocked(listTodosLocal).mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("Diario page has no axe violations in the no-run empty state", async () => {
    const { container } = render(<DiarioPage />);

    await waitFor(() => {
      expect(listBrainRuns).toHaveBeenCalled();
    });

    expect((await axe(container)).violations).toEqual([]);
  });

  it("Todos page has no axe violations in the empty state", async () => {
    const { container } = render(<TodosPage />);

    await waitFor(() => {
      expect(listTodosLocal).toHaveBeenCalled();
    });

    expect((await axe(container)).violations).toEqual([]);
  });

  it("Tasks page has no axe violations in the empty kanban state", async () => {
    const { container } = render(<TasksPage />);

    await waitFor(() => {
      expect(listTasks).toHaveBeenCalled();
    });

    expect((await axe(container)).violations).toEqual([]);
  });
});
