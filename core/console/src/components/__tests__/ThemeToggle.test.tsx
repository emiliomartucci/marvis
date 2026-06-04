import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const setTheme = vi.fn();
const mockUseTheme = vi.fn();

vi.mock("next-themes", () => ({
  useTheme: () => mockUseTheme(),
}));

import { ThemeToggle } from "../ui/ThemeToggle";

describe("ThemeToggle", () => {
  beforeEach(() => {
    setTheme.mockReset();
    mockUseTheme.mockReset();
  });

  it("toggles to light when system currently resolves dark", async () => {
    const user = userEvent.setup();
    mockUseTheme.mockReturnValue({
      theme: "system",
      resolvedTheme: "dark",
      setTheme,
    });

    render(<ThemeToggle />);

    const button = await screen.findByRole("button", { name: "Switch theme (current: system (dark))" });
    await user.click(button);

    expect(setTheme).toHaveBeenCalledWith("light");
  });

  it("toggles to dark when the effective theme is light", async () => {
    const user = userEvent.setup();
    mockUseTheme.mockReturnValue({
      theme: "light",
      resolvedTheme: "light",
      setTheme,
    });

    render(<ThemeToggle />);

    const button = await screen.findByRole("button", { name: "Switch theme (current: light)" });
    await user.click(button);

    expect(setTheme).toHaveBeenCalledWith("dark");
  });
});
