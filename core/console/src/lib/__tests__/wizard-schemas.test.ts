import { describe, expect, it } from "vitest";

import {
  maskApiKey,
  slugify,
  SKIPPABLE_STEPS,
  STEP_ORDER,
  validateFirstProject,
  validateLlmProvider,
  validateStorage,
  validateWelcome,
} from "../wizard-schemas";

describe("step order + skippable set", () => {
  it("matches the canonical order", () => {
    expect(STEP_ORDER).toEqual([
      "welcome",
      "storage",
      "llm_provider",
      "first_project",
      "recap",
    ]);
  });

  it("only storage / llm / first_project are skippable", () => {
    expect(SKIPPABLE_STEPS.has("storage")).toBe(true);
    expect(SKIPPABLE_STEPS.has("llm_provider")).toBe(true);
    expect(SKIPPABLE_STEPS.has("first_project")).toBe(true);
    expect(SKIPPABLE_STEPS.has("welcome")).toBe(false);
    expect(SKIPPABLE_STEPS.has("recap")).toBe(false);
  });
});

describe("validators", () => {
  it("welcome rejects unaccepted license", () => {
    const errors = validateWelcome({
      bsl_accepted: false,
      accepted_at: null,
    });
    expect(errors.map((e) => e.field)).toContain("bsl_accepted");
  });

  it("welcome accepts when checked", () => {
    expect(
      validateWelcome({ bsl_accepted: true, accepted_at: null })
    ).toEqual([]);
  });

  it("storage rejects relative paths", () => {
    const errors = validateStorage({
      projects_root: "relative/path",
      db_backend: "sqlite",
      db_path: "/tmp/x.db",
      postgres_dsn: null,
    });
    expect(errors.map((e) => e.field)).toContain("projects_root");
  });

  it("storage postgres requires DSN", () => {
    const errors = validateStorage({
      projects_root: "/abs/path",
      db_backend: "postgres",
      db_path: null,
      postgres_dsn: null,
    });
    expect(errors.map((e) => e.field)).toContain("postgres_dsn");
  });

  it("llm allow-empty skip path", () => {
    expect(
      validateLlmProvider(
        {
          provider: null,
          api_key: null,
          base_url: null,
          test_passed: false,
        },
        { allowEmpty: true }
      )
    ).toEqual([]);
  });

  it("llm mac_gateway needs base_url + api_key", () => {
    const errors = validateLlmProvider({
      provider: "mac_gateway",
      api_key: null,
      base_url: null,
      test_passed: false,
    });
    const fields = new Set(errors.map((e) => e.field));
    expect(fields).toEqual(new Set(["base_url", "api_key"]));
  });

  it("first_project requires valid slug", () => {
    const errors = validateFirstProject({
      name: "x",
      slug: "Bad Slug!",
      type: "code",
    });
    expect(errors.map((e) => e.field)).toContain("slug");
  });

  it("first_project accepts valid", () => {
    expect(
      validateFirstProject({ name: "x", slug: "ok-slug", type: "code" })
    ).toEqual([]);
  });

  it("first_project accepts single-character slug", () => {
    expect(validateFirstProject({ name: "x", slug: "a", type: "code" })).toEqual(
      []
    );
  });
});

describe("slugify", () => {
  it.each([
    ["My First Project!", "my-first-project"],
    ["  Multi   Space  ", "multi-space"],
    ["", ""],
    ["Already-good-slug", "already-good-slug"],
  ])("slugify(%j) === %j", (input, expected) => {
    expect(slugify(input)).toBe(expected);
  });
});

describe("maskApiKey", () => {
  it("empty and short cases", () => {
    expect(maskApiKey(null)).toBe("");
    expect(maskApiKey("")).toBe("");
    expect(maskApiKey("short")).toBe("***");
  });

  it("first 6 + last 4 for normal keys", () => {
    expect(maskApiKey("sk-ant-abcdefghij1234")).toBe("sk-ant***1234");
  });
});
