import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  usePathname: () => "/settings/llm",
}));

import SettingsSidebar from "../SettingsSidebar";

afterEach(() => {
  cleanup();
  delete process.env.NEXT_PUBLIC_LOCAL_MODE;
});

describe("SettingsSidebar local-mode gating (gh #33)", () => {
  it("hides Teams and Roles & Permissions in local single-user mode", () => {
    process.env.NEXT_PUBLIC_LOCAL_MODE = "1";
    render(<SettingsSidebar />);
    expect(screen.queryByText("Teams")).toBeNull();
    expect(screen.queryByText("Roles & Permissions")).toBeNull();
    // Users stays — single-user still has the local operator.
    expect(screen.getByText("Users")).toBeTruthy();
  });

  it("shows Teams and Roles & Permissions in managed/multi-user mode", () => {
    process.env.NEXT_PUBLIC_LOCAL_MODE = "0";
    render(<SettingsSidebar />);
    expect(screen.getByText("Teams")).toBeTruthy();
    expect(screen.getByText("Roles & Permissions")).toBeTruthy();
  });
});
