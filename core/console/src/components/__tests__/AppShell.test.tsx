import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { SESSION_COUNT_CHANGED_EVENT } from "@/lib/sessionEvents";

let mockedPathname = "/projects/";
let mockedDesignV2 = false;
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
  listSessions: vi.fn(async () => []),
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

vi.mock("@/lib/useDesignV2", () => ({
  useDesignV2: () => mockedDesignV2,
}));

import AppShell from "../AppShell";
import { createTodoLocal, listSessions, listTodosLocal } from "@/lib/api";

describe("AppShell", () => {
  beforeEach(() => {
    mockedPathname = "/projects/";
    mockedDesignV2 = false;
    delete process.env.NEXT_PUBLIC_LOCAL_MODE;
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

  it("renders top bar with package navigation", () => {
    render(
      <AppShell>
        <div>Content</div>
      </AppShell>
    );
    expect(screen.getByText("Terminal")).toBeInTheDocument();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(screen.getByText("Logout")).toBeInTheDocument();
  });

  it("exposes Inbox RSS and Ingester menu entries", () => {
    render(
      <AppShell>
        <div>Content</div>
      </AppShell>
    );

    expect(screen.getByRole("menu", { name: "Inbox navigation" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "RSS" })).toHaveAttribute("href", "/inbox");
    expect(screen.getByRole("menuitem", { name: "Ingester" })).toHaveAttribute("href", "/inbox/triage/files");
  });

  it("renders children in content area", () => {
    render(
      <AppShell>
        <div>Test Content</div>
      </AppShell>
    );
    expect(screen.getByText("Test Content")).toBeInTheDocument();
  });

  it("renders sidebar slot when provided", () => {
    render(
      <AppShell sidebar={<div>Sidebar Content</div>}>
        <div>Main</div>
      </AppShell>
    );
    expect(screen.getByText("Sidebar Content")).toBeInTheDocument();
  });

  it("has app-shell testid", () => {
    const { container } = render(
      <AppShell sidebar={<div>Side</div>}>
        <div>Main</div>
      </AppShell>
    );
    const shell = container.querySelector("[data-testid='app-shell']");
    expect(shell).toBeInTheDocument();
  });

  it("renders the local-mode sidebar and hides the auth cluster when the flag is on", async () => {
    process.env.NEXT_PUBLIC_LOCAL_MODE = "1";
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

  it("wires local quick-capture to the todos endpoint", async () => {
    process.env.NEXT_PUBLIC_LOCAL_MODE = "1";
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

  it("shows the open todos badge in local mode", async () => {
    process.env.NEXT_PUBLIC_LOCAL_MODE = "1";
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
    process.env.NEXT_PUBLIC_LOCAL_MODE = "1";
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

  it("updates terminal session count from TerminalPanel count events without refetching on sessions_changed", async () => {
    mockedPathname = "/terminal/";
    mockedDesignV2 = true;

    render(
      <AppShell>
        <div>Main</div>
      </AppShell>
    );

    expect(await screen.findByText("Sessioni")).toBeInTheDocument();
    const initialFetches = vi.mocked(listSessions).mock.calls.length;

    act(() => {
      window.dispatchEvent(new CustomEvent(SESSION_COUNT_CHANGED_EVENT, { detail: { count: 12 } }));
    });
    expect(await screen.findByText("12")).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new CustomEvent("marvisx:sessions_changed"));
    });
    expect(vi.mocked(listSessions).mock.calls.length).toBe(initialFetches);
  });
});
