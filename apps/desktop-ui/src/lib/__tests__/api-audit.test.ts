import { afterEach, describe, expect, it, vi } from "vitest";

import { APIError, getAuditLog } from "../api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function mockJsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("getAuditLog", () => {
  it("calls the real audit endpoint and maps backend rows for the Activity UI", async () => {
    const fetch = vi.fn().mockResolvedValue(mockJsonResponse([
      {
        id: "a1",
        timestamp: "2026-06-19T20:00:00.000Z",
        action: "pr.merge",
        user: "emilio",
        resource_type: "pull_request",
        resource_id: "39",
        details: { branch: "fix/audit" },
      },
      {
        id: "a2",
        timestamp: "2026-06-19T19:58:00.000Z",
        action: "tool_call",
        user: "codex",
        resource_type: "search",
        resource_id: "search",
        details: null,
      },
    ]));
    vi.stubGlobal("fetch", fetch);

    const result = await getAuditLog({ action: "pr.", limit: 2, offset: 20 });

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/audit?action=pr.&limit=2&offset=20",
      expect.objectContaining({ credentials: "include" })
    );
    expect(result).toEqual({
      entries: [
        {
          id: "a1",
          timestamp: "2026-06-19T20:00:00.000Z",
          user_id: "emilio",
          user_name: "emilio",
          event_type: "pr_merged",
          description: "pr.merge on pull_request 39",
          metadata: { branch: "fix/audit" },
        },
        {
          id: "a2",
          timestamp: "2026-06-19T19:58:00.000Z",
          user_id: "codex",
          user_name: "codex",
          event_type: "audit_entry",
          description: "tool_call on search search",
          metadata: null,
        },
      ],
      next_cursor: "22",
      total: 22,
    });
  });

  it("keeps the backend admin-only 403 readable for the page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      mockJsonResponse({ detail: "Insufficient permissions" }, { status: 403 })
    ));

    await expect(getAuditLog()).rejects.toMatchObject({
      name: "APIError",
      status: 403,
      message: "Insufficient permissions",
    } satisfies Partial<APIError>);
  });
});
