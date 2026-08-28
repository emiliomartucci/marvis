import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("diff2html", () => ({
  html: vi.fn(
    () =>
      '<img src="x" onerror="globalThis.__xss = true">' +
      '<svg><g onload="globalThis.__xss = true"></g></svg>' +
      '<script>globalThis.__xss = true</script>',
  ),
}));

import DiffViewer from "../DiffViewer";


describe("DiffViewer", () => {
  it("removes executable markup before inserting generated diff HTML", () => {
    const { container } = render(<DiffViewer unifiedDiff="untrusted diff" />);

    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelector("[onload]")).toBeNull();
    expect((globalThis as { __xss?: boolean }).__xss).toBeUndefined();
  });
});
