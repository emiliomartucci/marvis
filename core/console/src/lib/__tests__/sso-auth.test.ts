import { afterEach, describe, expect, it, vi } from "vitest";

import {
  APIError,
  getMe,
  getSSOConfig,
  getSSOLoginUrl,
  startSSOLogin,
} from "../api";
import * as config from "../config";
import {
  CONSOLE_LOGIN_PATH,
  redirectToConsoleLogin,
} from "../config";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("hosted WorkOS browser auth contract", () => {
  it("keeps hard auth redirects inside the console base path", () => {
    const navigate = vi.fn();

    redirectToConsoleLogin(navigate);

    expect(CONSOLE_LOGIN_PATH).toBe("/ui/login/");
    expect(navigate).toHaveBeenCalledOnce();
    expect(navigate).toHaveBeenCalledWith("/ui/login/");
  });

  it("routes an anonymous console bootstrap to the base-path login page", async () => {
    const redirect = vi
      .spyOn(config, "redirectToConsoleLogin")
      .mockImplementation(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 401 }))
    );

    await expect(getMe()).rejects.toEqual(new APIError("Unauthorized", 401));

    expect(redirect).toHaveBeenCalledOnce();
  });

  it("queries the backend by workspace and preserves its public response", async () => {
    const fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        enabled: true,
        email_domains: [],
        provider: "workos",
      })
    );
    vi.stubGlobal("fetch", fetch);

    const result = await getSSOConfig("ws_default");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/auth/sso/config?workspace=ws_default",
      expect.objectContaining({ credentials: "include" })
    );
    expect(result).toEqual({
      enabled: true,
      email_domains: [],
      provider: "workos",
    });
  });

  it("builds a same-origin navigation URL without fetching through WorkOS", () => {
    const fetch = vi.fn().mockResolvedValue(
      jsonResponse({ redirect_url: "https://should-not-be-fetched.example" })
    );
    vi.stubGlobal("fetch", fetch);

    expect(getSSOLoginUrl("ws_default")).toBe(
      "/api/v1/auth/sso/login?workspace=ws_default"
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("navigates the browser through the same-origin SSO endpoint", () => {
    const navigate = vi.fn();

    startSSOLogin("ws_default", navigate);

    expect(navigate).toHaveBeenCalledOnce();
    expect(navigate).toHaveBeenCalledWith(
      "/api/v1/auth/sso/login?workspace=ws_default"
    );
  });
});
