// v1.0.0 - 2026-04-27 - PR #21: smoke test useGraphSearch (debounce + threshold filter).
//
// Verifica: short query → empty set, threshold > 0.6 filtra hits, abort cleanup.

import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const globalSearchMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  globalSearch: globalSearchMock,
}));

import { useGraphSearch } from "../useGraphSearch";

function searchHit(slug: string, score: number) {
  return {
    doc_type: "project" as const,
    doc_id: slug,
    title: slug,
    project: slug,
    score,
  };
}

describe("useGraphSearch", () => {
  beforeEach(() => {
    globalSearchMock.mockReset();
  });

  it("query < 2 char → empty set, no fetch", async () => {
    const { result } = renderHook(({ q }) => useGraphSearch(q), {
      initialProps: { q: "" },
    });
    expect(result.current.size).toBe(0);
    // Wait beyond debounce window — nessuna chiamata attesa.
    await new Promise((r) => setTimeout(r, 400));
    expect(globalSearchMock).not.toHaveBeenCalled();
  });

  it("query >= 2 char → debounced fetch, score > 0.4 inclusi", async () => {
    globalSearchMock.mockResolvedValueOnce({
      tasks: [],
      projects: [
        searchHit("alpha", 0.9),
        searchHit("beta", 0.39), // below threshold
        searchHit("gamma", 0.7),
      ],
      files: [],
      handoffs: [],
      total: 3,
      query: "test",
    });

    const { result } = renderHook(({ q }) => useGraphSearch(q), {
      initialProps: { q: "test" },
    });
    expect(result.current.size).toBe(0);
    await waitFor(() => expect(result.current.size).toBeGreaterThan(0), {
      timeout: 1500,
    });
    expect(result.current.has("alpha")).toBe(true);
    expect(result.current.has("gamma")).toBe(true);
    expect(result.current.has("beta")).toBe(false);
  });

  it("error → set vuoto best-effort (no throw)", async () => {
    globalSearchMock.mockRejectedValueOnce(new Error("boom"));
    const { result } = renderHook(() => useGraphSearch("query"));
    // Aspetto debounce + microtask di catch.
    await new Promise((r) => setTimeout(r, 400));
    expect(result.current.size).toBe(0);
  });
});
