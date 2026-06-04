import { beforeEach, describe, expect, it } from "vitest";

import {
  buildActions,
  createWizardStore,
  initialWizardState,
  stepStatus,
} from "../wizard-store";

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
    removeItem: (key: string) => {
      map.delete(key);
    },
  };
}

describe("wizard-store", () => {
  let store: ReturnType<typeof createWizardStore>;
  let actions: ReturnType<typeof buildActions>;

  beforeEach(() => {
    store = createWizardStore({ storage: memoryStorage() });
    actions = buildActions(store);
  });

  it("initial state lands on welcome", () => {
    const state = store.getState();
    expect(state.current_step).toBe("welcome");
    expect(state.completed_steps).toEqual([]);
    expect(state.skipped_steps).toEqual([]);
    expect(state.completed_at).toBeNull();
  });

  it("advance progresses through steps", () => {
    actions.advance();
    expect(store.getState().current_step).toBe("storage");
    expect(store.getState().completed_steps).toContain("welcome");

    actions.advance();
    expect(store.getState().current_step).toBe("llm_provider");
  });

  it("advance from recap stays on recap and marks completed", () => {
    actions.jumpTo("recap");
    actions.advance();
    expect(store.getState().current_step).toBe("recap");
    expect(store.getState().completed_steps).toContain("recap");
  });

  it("goBack reverses one step at a time", () => {
    actions.jumpTo("llm_provider");
    actions.goBack();
    expect(store.getState().current_step).toBe("storage");
    actions.goBack();
    expect(store.getState().current_step).toBe("welcome");
    actions.goBack();
    expect(store.getState().current_step).toBe("welcome");
  });

  it("skipCurrent only allowed for skippable steps", () => {
    expect(() => actions.skipCurrent()).toThrowError(/cannot be skipped/);
    actions.jumpTo("storage");
    actions.skipCurrent();
    expect(store.getState().skipped_steps).toContain("storage");
    expect(store.getState().current_step).toBe("llm_provider");
  });

  it("completing a step removes it from skipped", () => {
    actions.jumpTo("storage");
    actions.skipCurrent();
    expect(store.getState().skipped_steps).toContain("storage");
    actions.jumpTo("storage");
    actions.advance();
    const state = store.getState();
    expect(state.skipped_steps).not.toContain("storage");
    expect(state.completed_steps).toContain("storage");
  });

  it("setWelcome merges patch into welcome payload", () => {
    actions.setWelcome({ bsl_accepted: true });
    expect(store.getState().welcome.bsl_accepted).toBe(true);
  });

  it("setStorage / setLlmProvider / setFirstProject persist payload", () => {
    actions.setStorage({
      projects_root: "/tmp/projects",
      db_backend: "sqlite",
      db_path: "/tmp/db.sqlite",
      postgres_dsn: null,
    });
    actions.setLlmProvider({
      provider: "anthropic",
      api_key: "sk-ant-abc",
      base_url: null,
      test_passed: true,
    });
    actions.setFirstProject({
      name: "x",
      slug: "x-slug",
      type: "code",
    });

    const state = store.getState();
    expect(state.storage?.projects_root).toBe("/tmp/projects");
    expect(state.llm_provider?.provider).toBe("anthropic");
    expect(state.first_project?.slug).toBe("x-slug");
  });

  it("finalize stamps completed_at", () => {
    actions.jumpTo("recap");
    actions.finalize();
    const state = store.getState();
    expect(state.completed_at).not.toBeNull();
    expect(state.completed_steps).toContain("recap");
  });

  it("reset returns store to initial state and clears storage", () => {
    actions.setWelcome({ bsl_accepted: true });
    actions.advance();
    actions.reset();
    const state = store.getState();
    expect(state.current_step).toBe("welcome");
    expect(state.welcome.bsl_accepted).toBe(false);
  });

  it("subscribe is invoked on state change and unsubscribe stops calls", () => {
    let calls = 0;
    const unsub = store.subscribe(() => {
      calls += 1;
    });
    actions.advance();
    actions.advance();
    expect(calls).toBe(2);
    unsub();
    actions.advance();
    expect(calls).toBe(2);
  });

  it("state survives across store rehydration via shared storage", () => {
    const shared = memoryStorage();
    const a = createWizardStore({ storage: shared });
    buildActions(a).advance();
    buildActions(a).setWelcome({ bsl_accepted: true });

    const b = createWizardStore({ storage: shared });
    expect(b.getState().current_step).toBe("storage");
    expect(b.getState().welcome.bsl_accepted).toBe(true);
  });

  it("corrupted storage falls back to fresh state", () => {
    const shared = memoryStorage();
    shared.setItem("marvisx:wizard-state:v1", "{not json");
    const recovered = createWizardStore({ storage: shared });
    expect(recovered.getState().current_step).toBe("welcome");
  });

  it("initialWizardState produces deterministic shape", () => {
    const a = initialWizardState();
    expect(a.version).toBe("1.0");
    expect(a.current_step).toBe("welcome");
    expect(a.completed_steps).toEqual([]);
  });
});

describe("stepStatus", () => {
  it("returns active for current step until finalize", () => {
    const state = initialWizardState();
    expect(stepStatus(state, "welcome")).toBe("active");
    expect(stepStatus(state, "storage")).toBe("pending");
  });

  it("returns completed if step in completed_steps", () => {
    const state = { ...initialWizardState(), completed_steps: ["welcome" as const] };
    expect(stepStatus(state, "welcome")).toBe("completed");
  });

  it("returns skipped if step in skipped_steps", () => {
    const state = {
      ...initialWizardState(),
      skipped_steps: ["storage" as const],
    };
    expect(stepStatus(state, "storage")).toBe("skipped");
  });

  it("returns pending when finalized but step never reached", () => {
    const state = {
      ...initialWizardState(),
      completed_at: new Date().toISOString(),
      current_step: "recap" as const,
    };
    // Current step recap not in completed_steps so still considered pending under "active gate"
    expect(stepStatus(state, "welcome")).toBe("pending");
  });
});
