import assert from "node:assert/strict";
import test from "node:test";

import { resolveConsoleBuildId } from "./console-build-id.mjs";

const SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567";

test("returns the exact source SHA supplied by a release build", () => {
  assert.equal(
    resolveConsoleBuildId({ MARVIS_CONSOLE_BUILD_ID: SOURCE_SHA }),
    SOURCE_SHA,
  );
});

test("rejects a malformed source revision", () => {
  assert.throws(
    () => resolveConsoleBuildId({ MARVIS_CONSOLE_BUILD_ID: "main" }),
    /exact 40-character lowercase Git SHA/,
  );
});

test("fails closed when CI omits the source revision", () => {
  assert.throws(
    () => resolveConsoleBuildId({ CI: "true" }),
    /required for reproducible CI builds/,
  );
});

test("uses one stable identifier for interactive local builds", () => {
  assert.equal(resolveConsoleBuildId({}), "local");
});
