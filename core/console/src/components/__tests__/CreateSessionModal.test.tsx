import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { createSession, getPrograms, getSessionCatalog, mockUseTheme } = vi.hoisted(() => ({
  createSession: vi.fn(),
  getPrograms: vi.fn(),
  getSessionCatalog: vi.fn(),
  mockUseTheme: vi.fn(),
}));

vi.mock("next-themes", () => ({
  useTheme: () => mockUseTheme(),
}));

vi.mock("@/lib/api", () => ({
  createSession,
  getPrograms,
  getSessionCatalog,
}));

import CreateSessionModal from "../CreateSessionModal";

describe("CreateSessionModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseTheme.mockReturnValue({ resolvedTheme: "light" });
    getPrograms.mockResolvedValue([]);
    getSessionCatalog.mockResolvedValue({
      providers: [
        {
          id: "claude",
          label: "Claude",
          default_model: "opus",
          launch_root: "workspace",
          models: [
            {
              id: "",
              label: "Blank",
              description: "Skip the model flag and let the CLI choose.",
              context_window: null,
              supports_1m: false,
              recommended: false,
              experimental: false,
              note: null,
            },
            {
              id: "opus",
              label: "Opus",
              description: "Default Claude model",
              context_window: 200000,
              supports_1m: false,
              recommended: true,
              experimental: false,
              note: null,
            },
          ],
          permission_presets: [],
          note: null,
        },
      ],
    });
    createSession.mockResolvedValue({});
  });

  it("sends the resolved theme with new session creation", async () => {
    const user = userEvent.setup();

    render(
      <CreateSessionModal onClose={vi.fn()} onCreated={vi.fn()} />
    );

    await waitFor(() => {
      expect(getSessionCatalog).toHaveBeenCalled();
    });

    await user.type(screen.getByPlaceholderText("my-session"), "theme-test");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(createSession).toHaveBeenCalledWith({
        name: "theme-test",
        provider: "claude",
        model: "opus",
        permission_preset: undefined,
        project_slug: undefined,
        theme_mode: "light",
      });
    });
  });

  it("submits an explicit blank model selection", async () => {
    const user = userEvent.setup();

    render(
      <CreateSessionModal onClose={vi.fn()} onCreated={vi.fn()} />
    );

    await waitFor(() => {
      expect(getSessionCatalog).toHaveBeenCalled();
    });

    await user.type(screen.getByPlaceholderText("my-session"), "blank-test");
    await user.click(screen.getByRole("button", { name: /Blank/ }));
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(createSession).toHaveBeenCalledWith({
        name: "blank-test",
        provider: "claude",
        model: "",
        permission_preset: undefined,
        project_slug: undefined,
        theme_mode: "light",
      });
    });
  });
});
