// Test per hook persistenza view state (PR #2).
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __test__, useGraphViewState } from "../useGraphViewState";
import { DEFAULT_VIEW_STATE } from "../types";

const { LS_KEY, LS_DEBOUNCE_MS, MAX_NODE_OVERRIDES, loadFromLS, sanitize } =
  __test__;

describe("useGraphViewState::loadFromLS", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("LS empty → DEFAULT", () => {
    expect(loadFromLS()).toEqual(DEFAULT_VIEW_STATE);
  });

  it("LS valid → parsed", () => {
    const state = {
      zoom: 1.5,
      pan: { x: 10, y: 20 },
      nodeOverrides: { alpha: { x: 1, y: 2 } },
    };
    localStorage.setItem(LS_KEY, JSON.stringify(state));
    const loaded = loadFromLS();
    expect(loaded.zoom).toBe(1.5);
    expect(loaded.pan).toEqual({ x: 10, y: 20 });
    expect(loaded.nodeOverrides.alpha).toEqual({ x: 1, y: 2 });
  });

  it("LS corrupt JSON → DEFAULT, no throw", () => {
    localStorage.setItem(LS_KEY, "{not json");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(loadFromLS()).toEqual(DEFAULT_VIEW_STATE);
    warn.mockRestore();
  });

  it("LS zoom out-of-bounds → DEFAULT + console.warn", () => {
    localStorage.setItem(
      LS_KEY,
      JSON.stringify({
        zoom: 1000,
        pan: { x: 0, y: 0 },
        nodeOverrides: {},
      }),
    );
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(loadFromLS()).toEqual(DEFAULT_VIEW_STATE);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe("useGraphViewState::sanitize", () => {
  it("rimuove chiavi `__proto__` / `constructor` / `prototype`", () => {
    // Cast via unknown per evitare il ban di `any` (test-only pollution).
    const pollutedOverrides: Record<string, { x: number; y: number }> = {
      alpha: { x: 1, y: 2 },
    };
    (pollutedOverrides as unknown as Record<string, { x: number; y: number }>)[
      "__proto__"
    ] = { x: 9, y: 9 };
    (pollutedOverrides as unknown as Record<string, { x: number; y: number }>)[
      "constructor"
    ] = { x: 9, y: 9 };
    (pollutedOverrides as unknown as Record<string, { x: number; y: number }>)[
      "prototype"
    ] = { x: 9, y: 9 };
    const polluted = {
      zoom: 1,
      pan: { x: 0, y: 0 },
      nodeOverrides: pollutedOverrides,
    };
    const clean = sanitize(polluted);
    expect(Object.keys(clean.nodeOverrides)).toEqual(["alpha"]);
  });

  it("cappa nodeOverrides a 500 entries", () => {
    const big = { zoom: 1, pan: { x: 0, y: 0 }, nodeOverrides: {} as Record<string, {x:number;y:number}> };
    for (let i = 0; i < 600; i++) big.nodeOverrides[`k${i}`] = { x: i, y: i };
    const clean = sanitize(big);
    expect(Object.keys(clean.nodeOverrides).length).toBe(MAX_NODE_OVERRIDES);
  });
});

describe("useGraphViewState::hook", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("patch({zoom}) update state; altri campi preservati", () => {
    const { result } = renderHook(() => useGraphViewState());
    expect(result.current[0].zoom).toBe(DEFAULT_VIEW_STATE.zoom);
    act(() => {
      result.current[1]({ zoom: 2 });
    });
    expect(result.current[0].zoom).toBe(2);
    expect(result.current[0].pan).toEqual({ x: 0, y: 0 });
  });

  it("LS write debounced 250ms", () => {
    const { result } = renderHook(() => useGraphViewState());
    act(() => {
      result.current[1]({ zoom: 3 });
    });
    // Niente scritta prima del debounce.
    expect(localStorage.getItem(LS_KEY)).toBeNull();
    act(() => {
      vi.advanceTimersByTime(LS_DEBOUNCE_MS + 10);
    });
    const written = localStorage.getItem(LS_KEY);
    expect(written).not.toBeNull();
    const parsed = JSON.parse(written ?? "{}");
    expect(parsed.zoom).toBe(3);
  });
});
