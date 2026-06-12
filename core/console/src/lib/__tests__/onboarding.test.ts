import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteOnboardingDemo,
  seedOnboardingDemo,
} from "../api";
import {
  DEMO_SEEDED_STORAGE_KEY,
  ONBOARDED_STORAGE_KEY,
  brainSourcesSetupContent,
  identitySetupContent,
  markDemoRemoved,
  markDemoSeeded,
  markOnboardingDone,
  parseExclusions,
  rhythmSetupContent,
  shouldShowOnboarding,
  sourcesSetupContent,
} from "../onboarding";

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
}

describe("onboarding first-run gate", () => {
  it("shows the wizard only when onboarding is not done and demo is not seeded", () => {
    const storage = memoryStorage();

    expect(shouldShowOnboarding(storage)).toBe(true);

    markOnboardingDone(storage);
    expect(storage.getItem(ONBOARDED_STORAGE_KEY)).toBe("true");
    expect(shouldShowOnboarding(storage)).toBe(false);
  });

  it("does not show the wizard while demo data is already seeded", () => {
    const storage = memoryStorage();

    markDemoSeeded(storage);
    expect(storage.getItem(DEMO_SEEDED_STORAGE_KEY)).toBe("true");
    expect(shouldShowOnboarding(storage)).toBe(false);

    markDemoRemoved(storage);
    expect(storage.getItem(DEMO_SEEDED_STORAGE_KEY)).toBeNull();
    expect(shouldShowOnboarding(storage)).toBe(true);
  });

  it("accepts legacy truthy storage values", () => {
    const storage = memoryStorage();
    storage.setItem(ONBOARDED_STORAGE_KEY, "1");

    expect(shouldShowOnboarding(storage)).toBe(false);
  });
});

describe("onboarding setup helpers", () => {
  it("normalizes explicit exclusions from comma and newline input", () => {
    expect(parseExclusions("node_modules, dist\n_archive\n\n")).toEqual([
      "node_modules",
      "dist",
      "_archive",
    ]);
  });

  it("renders authored setup sections without derived project state", () => {
    expect(identitySetupContent({ operator: " Emilio ", company: " MarvisX " })).toBe(
      "operatore: Emilio\nazienda: MarvisX",
    );
    expect(
      sourcesSetupContent({
        root: "/work",
        sources: ["client-a", "repo-b"],
        exclusions: ["node_modules"],
      }),
    ).toContain("indicizza:\n- client-a\n- repo-b\nescludi:\n- node_modules");
    expect(rhythmSetupContent({ cycleHour: "" })).toContain("ciclo_brain: 03:00");
    expect(brainSourcesSetupContent({ docsConsent: true, repoConsent: false })).toBe(
      "locali: documenti\nenterprise_previste: email, knowledge base, gestionale",
    );
  });
});

describe("onboarding demo API", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  it("POSTs the Casa Lorenzi demo seed with the active locale", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          project: "casa-lorenzi",
          created: true,
          tasks: ["task-1"],
          todos: ["todo-1"],
          lang: "en",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(seedOnboardingDemo("en")).resolves.toMatchObject({
      project: "casa-lorenzi",
      lang: "en",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/onboarding/demo?lang=en",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ lang: "en" }),
      }),
    );
  });

  it("DELETEs only demo-tagged onboarding data", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          project: "casa-lorenzi",
          tasks_deleted: 2,
          todos_deleted: 3,
          project_deleted: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(deleteOnboardingDemo()).resolves.toMatchObject({
      project: "casa-lorenzi",
      project_deleted: true,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/onboarding/demo",
      expect.objectContaining({
        method: "DELETE",
        credentials: "include",
      }),
    );
  });
});
