import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

import AppLayout from "../layout";

describe("AppLayout", () => {
  it("renders the page inside the shell", () => {
    render(
      <AppLayout>
        <div>Page content</div>
      </AppLayout>
    );

    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
    expect(screen.getByText("Page content")).toBeInTheDocument();
  });

  // The layout used to mount TerminalPanel on every route so terminal session
  // state survived navigation. The terminal belongs to marvisx: mounting it here
  // pulled terminal code into the local bundle even after the route was pruned.
  it("does not mount the terminal panel", () => {
    const { container } = render(
      <AppLayout>
        <div>Page content</div>
      </AppLayout>
    );

    expect(container.querySelector('[data-testid="terminal-panel"]')).toBeNull();
  });
});
