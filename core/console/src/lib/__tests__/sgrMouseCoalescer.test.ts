import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SgrMouseCoalescer, isSgrWheelInput } from "../sgrMouseCoalescer";

describe("SgrMouseCoalescer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("coalesces wheel bursts to one dispatch per tick", () => {
    const dispatched: string[] = [];
    const coalescer = new SgrMouseCoalescer({
      dispatchMs: 16,
      onDispatch: (data) => dispatched.push(data),
    });

    for (let index = 0; index < 100; index++) {
      coalescer.push(`\x1b[<64;1;${index}M`);
      vi.advanceTimersByTime(1);
    }
    vi.runOnlyPendingTimers();

    expect(dispatched.length).toBeGreaterThanOrEqual(6);
    expect(dispatched.length).toBeLessThanOrEqual(7);
    expect(dispatched.at(-1)).toBe("\x1b[<64;1;99M");
  });

  it("passes non-wheel mouse and text input through immediately", () => {
    const dispatched: string[] = [];
    const coalescer = new SgrMouseCoalescer({
      dispatchMs: 16,
      onDispatch: (data) => dispatched.push(data),
    });

    coalescer.push("\x1b[<0;10;10M");
    coalescer.push("a");

    expect(dispatched).toEqual(["\x1b[<0;10;10M", "a"]);
  });

  it("flushes a pending wheel input on dispose", () => {
    const dispatched: string[] = [];
    const coalescer = new SgrMouseCoalescer({
      dispatchMs: 16,
      onDispatch: (data) => dispatched.push(data),
    });

    coalescer.push("\x1b[<65;10;10M");
    coalescer.dispose();

    expect(dispatched).toEqual(["\x1b[<65;10;10M"]);
  });

  it("only treats SGR wheel button codes as wheel input", () => {
    expect(isSgrWheelInput("\x1b[<64;10;10M")).toBe(true);
    expect(isSgrWheelInput("\x1b[<65;10;10M")).toBe(true);
    expect(isSgrWheelInput("\x1b[<0;10;10M")).toBe(false);
    expect(isSgrWheelInput("hello")).toBe(false);
  });
});
