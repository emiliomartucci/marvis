import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { SESSION_COUNT_CHANGED_EVENT } from "@/lib/sessionEvents";

let mockedPathname = "/projects/";
let mockedDesignV2 = false;

vi.mock("@/lib/auth", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({ status: "authenticated", logout: vi.fn(), permissions: { canAdmin: false, canOperate: false } }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => mockedPathname,
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  listSessions: vi.fn(async () => []),
}));

vi.mock("@/lib/useDesignV2", () => ({
  useDesignV2: () => mockedDesignV2,
}));

import AppShell from "../AppShell";
import { listSessions } from "@/lib/api";

describe("AppShell", () => {
  beforeEach(() => {
    mockedPathname = "/projects/";
    mockedDesignV2 = false;
    vi.clearAllMocks();
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
