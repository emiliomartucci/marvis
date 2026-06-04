import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const usePathnameMock = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => usePathnameMock(),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/TerminalPanel", () => ({
  default: ({ panelVisible }: { panelVisible: boolean }) => (
    <div data-testid="terminal-panel">{panelVisible ? "visible" : "hidden"}</div>
  ),
}));

import AppLayout from "../layout";

describe("AppLayout", () => {
  beforeEach(() => {
    usePathnameMock.mockReset();
  });

  it("renders only the terminal panel on terminal routes", () => {
    usePathnameMock.mockReturnValue("/terminal/session-1");

    const { container } = render(
      <AppLayout>
        <div>Page content</div>
      </AppLayout>
    );

    expect(screen.getByTestId("terminal-panel")).toHaveTextContent("visible");
    expect(screen.getByText("Page content")).toBeInTheDocument();
    expect(container.querySelector(".flex-1.min-h-0.flex-col")).toHaveStyle({ display: "none" });
  });

  it("keeps the terminal panel mounted but suspended outside terminal routes", () => {
    usePathnameMock.mockReturnValue("/triage");

    render(
      <AppLayout>
        <div>Page content</div>
      </AppLayout>
    );

    expect(screen.getByTestId("terminal-panel")).toHaveTextContent("hidden");
    expect(screen.getByText("Page content")).toBeInTheDocument();
  });
});
