import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import LoginPage from "../page";
import { getSSOConfig, startSSOLogin } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  login: vi.fn(),
  getSSOConfig: vi.fn(),
  getSSOLoginUrl: vi.fn(
    () => "/api/v1/auth/sso/login?workspace=ws_default"
  ),
  startSSOLogin: vi.fn(),
}));

describe("hosted login page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("offers WorkOS immediately when the workspace provider is enabled", async () => {
    vi.mocked(getSSOConfig).mockResolvedValue({
      enabled: true,
      email_domains: [],
      provider: "workos",
    });

    render(<LoginPage />);

    const button = await screen.findByTestId("sso-login-button");
    expect(button.textContent).toContain("Sign in with WorkOS");
    expect(screen.queryByTestId("email-input")).toBeNull();

    fireEvent.click(button);

    expect(startSSOLogin).toHaveBeenCalledOnce();
    expect(startSSOLogin).toHaveBeenCalledWith("ws_default");
  });

  it("keeps password login only when the workspace has no SSO provider", async () => {
    vi.mocked(getSSOConfig).mockResolvedValue({
      enabled: false,
      email_domains: [],
      provider: null,
    });

    render(<LoginPage />);

    expect(await screen.findByTestId("email-input")).not.toBeNull();
  });

  it("fails closed when workspace discovery is unavailable", async () => {
    vi.mocked(getSSOConfig).mockRejectedValue(new Error("unavailable"));

    render(<LoginPage />);

    expect(await screen.findByRole("button", { name: "Retry" })).not.toBeNull();
    expect(screen.queryByTestId("email-input")).toBeNull();
  });
});
