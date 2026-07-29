import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

let mockedPathname = "/projects/";
let fetchMock: ReturnType<typeof vi.fn>;

function mockVersionResponse(version: {
  installed: string;
  latest: string | null;
  update_available: boolean;
}) {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify(version), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
  );
}

vi.mock("@/lib/auth", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({ status: "authenticated", logout: vi.fn(), permissions: { canAdmin: false, canOperate: false } }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => mockedPathname,
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  getPrograms: vi.fn(async () => []),
  listTodosLocal: vi.fn(async () => []),
  createTodoLocal: vi.fn(async () => ({
    id: "todo-1",
    type: "promemoria",
    family: "captured",
    status: "aperto",
    text: "Rivedere il piano",
    payload: null,
    fu: "2026-06-12",
    project: null,
    source: "user",
    source_ref: null,
    doer: "agent",
    linked_task_id: null,
    created_at: "2026-06-12T08:00:00Z",
    updated_at: "2026-06-12T08:00:00Z",
    resolved_at: null,
    virtual: false,
    origin: null,
  })),
}));

import AppShell from "../AppShell";
import { createTodoLocal, listTodosLocal } from "@/lib/api";

describe("AppShell", () => {
  beforeEach(() => {
    mockedPathname = "/projects/";
    localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(listTodosLocal).mockResolvedValue([]);
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    mockVersionResponse({
      installed: "0.3.8",
      latest: null,
      update_available: false,
    });
  });

  it("renders children in content area", () => {
    render(
      <AppShell>
        <div>Test Content</div>
      </AppShell>
    );
    expect(screen.getByText("Test Content")).toBeInTheDocument();
  });

  // The `sidebar` slot was hosted-only: LocalAppShellContent accepted the prop
  // and never rendered it, so the old test passed against the hosted branch and
  // said nothing about this product. The prop is gone with that branch.
  it("has app-shell testid", () => {
    const { container } = render(
      <AppShell>
        <div>Main</div>
      </AppShell>
    );
    expect(container.querySelector("[data-testid='app-shell']")).toBeInTheDocument();
  });

  it("renders the local sidebar without an auth cluster", async () => {
    mockedPathname = "/todos/";
    localStorage.setItem("marvis:locale", "it");

    render(
      <AppShell>
        <div>Content</div>
      </AppShell>
    );

    expect(screen.getByTestId("local-sidebar")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Aggiungi un promemoria…")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Navigazione locale" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Todos" })).toHaveAttribute("href", "/todos");
    expect(screen.queryByText("Logout")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Action view/)).not.toBeInTheDocument();

    expect(screen.getByTestId("local-status-bar")).toHaveTextContent("In locale");
    expect(screen.getByTestId("local-status-bar")).toHaveTextContent("localhost:8100");
    expect(await screen.findByText("v0.3.8")).toBeInTheDocument();
  });

  // The shell used to branch on NEXT_PUBLIC_LOCAL_MODE and, in the other branch,
  // navigate to Terminal / Triage / Brain / Inbox / Monitoring / Finder. The
  // flag hid that top bar; the bundle carried it anyway. There is one shell now,
  // and it must not reach any surface this product does not ship.
  it("navigates only to routes this product ships", () => {
    const { container } = render(
      <AppShell>
        <div>Content</div>
      </AppShell>
    );

    const shipped = new Set(["/diario", "/todos", "/tasks", "/projects", "/universe", "/settings/llm"]);
    const targets = Array.from(container.querySelectorAll("a[href^='/']"))
      .map((a) => (a.getAttribute("href") ?? "").replace(/\/$/, ""))
      .filter((href) => href !== "");

    expect(targets.length).toBeGreaterThan(0);
    for (const href of targets) {
      expect(shipped).toContain(href);
    }
  });

  it("wires local quick-capture to the todos endpoint", async () => {
    localStorage.setItem("marvis:locale", "it");

    render(
      <AppShell>
        <div>Content</div>
      </AppShell>
    );

    const input = screen.getByLabelText("Aggiungi un promemoria");
    fireEvent.change(input, { target: { value: "Rivedere il piano" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    expect(await screen.findByRole("status")).toHaveTextContent("Aggiunto");
    expect(vi.mocked(createTodoLocal)).toHaveBeenCalledWith({ text: "Rivedere il piano" });
  });

  it("shows the open todos badge", async () => {
    localStorage.setItem("marvis:locale", "it");
    vi.mocked(listTodosLocal).mockResolvedValue([
      {
        id: "todo-1",
        type: "promemoria",
        family: "captured",
        status: "aperto",
        text: "Rivedere il piano",
        payload: null,
        fu: "2026-06-12",
        project: null,
        source: "user",
        source_ref: null,
        doer: "agent",
        linked_task_id: null,
        created_at: "2026-06-12T08:00:00Z",
        updated_at: "2026-06-12T08:00:00Z",
        resolved_at: null,
        virtual: false,
        origin: null,
      },
    ]);

    render(
      <AppShell>
        <div>Content</div>
      </AppShell>
    );

    expect(await screen.findByLabelText("Todos aperti")).toHaveTextContent("1");
  });

  it("renders an update hint in the local status bar and copies the pip command", async () => {
    localStorage.setItem("marvis:locale", "it");
    mockVersionResponse({
      installed: "0.3.7",
      latest: "0.3.8",
      update_available: true,
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <AppShell>
        <div>Content</div>
      </AppShell>
    );

    fireEvent.click(await screen.findByRole("button", { name: "Copia comando di aggiornamento" }));

    expect(writeText).toHaveBeenCalledWith("pip install -U marvisx-cli");
    expect(await screen.findByText("Comando copiato")).toBeInTheDocument();
  });
});
