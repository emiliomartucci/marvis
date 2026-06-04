// Vanilla store + React hook + localStorage persist. Zustand-pattern, 0 ext deps.
// Wave 2 may swap to the zustand library transparently — the public API is identical.

"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import type {
  FirstProjectPayload,
  LlmProviderPayload,
  StepId,
  StoragePayload,
  WelcomePayload,
  WizardState,
} from "./wizard-schemas";
import { SKIPPABLE_STEPS, STEP_ORDER } from "./wizard-schemas";

const STORAGE_KEY = "marvisx:wizard-state:v1";

export function initialWizardState(): WizardState {
  return {
    version: "1.0",
    current_step: "welcome",
    completed_steps: [],
    skipped_steps: [],
    started_at: new Date().toISOString(),
    completed_at: null,
    welcome: { bsl_accepted: false, accepted_at: null },
    storage: null,
    llm_provider: null,
    first_project: null,
  };
}

type Listener = () => void;

export interface WizardStoreApi {
  getState: () => WizardState;
  setState: (updater: (prev: WizardState) => WizardState) => void;
  subscribe: (listener: Listener) => () => void;
  reset: () => void;
}

export function createWizardStore(
  options: {
    storage?: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
    storageKey?: string;
  } = {}
): WizardStoreApi {
  const key = options.storageKey ?? STORAGE_KEY;
  const storage =
    options.storage === undefined
      ? typeof window !== "undefined"
        ? window.localStorage
        : null
      : options.storage;

  let state: WizardState = initialWizardState();
  const listeners = new Set<Listener>();

  if (storage) {
    try {
      const raw = storage.getItem(key);
      if (raw) {
        const parsed = JSON.parse(raw);
        state = { ...initialWizardState(), ...parsed };
      }
    } catch {
      // Corrupted localStorage — keep fresh state.
    }
  }

  const persist = () => {
    if (!storage) return;
    try {
      storage.setItem(key, JSON.stringify(state));
    } catch {
      // Quota or unavailable — skip.
    }
  };

  return {
    getState: () => state,
    setState: (updater) => {
      state = updater(state);
      persist();
      listeners.forEach((listener) => listener());
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    reset: () => {
      state = initialWizardState();
      if (storage) {
        try {
          storage.removeItem(key);
        } catch {
          // Skip.
        }
      }
      listeners.forEach((listener) => listener());
    },
  };
}

// Singleton used by the React hook + actions in the app.
export const wizardStore: WizardStoreApi = createWizardStore();

// ---------------------------------------------------------------------------
// Pure-function actions over an arbitrary store — easy to unit test.
// ---------------------------------------------------------------------------

const indexOf = (step: StepId): number => STEP_ORDER.indexOf(step);

function withCompleted(state: WizardState, step: StepId): WizardState {
  const completed = state.completed_steps.includes(step)
    ? state.completed_steps
    : [...state.completed_steps, step];
  return {
    ...state,
    completed_steps: completed,
    skipped_steps: state.skipped_steps.filter((s) => s !== step),
  };
}

function withSkipped(state: WizardState, step: StepId): WizardState {
  const skipped = state.skipped_steps.includes(step)
    ? state.skipped_steps
    : [...state.skipped_steps, step];
  return {
    ...state,
    skipped_steps: skipped,
    completed_steps: state.completed_steps.filter((s) => s !== step),
  };
}

export function buildActions(store: WizardStoreApi) {
  return {
    advance() {
      store.setState((prev) => {
        const cleared = withCompleted(prev, prev.current_step);
        const idx = indexOf(prev.current_step);
        if (idx + 1 >= STEP_ORDER.length) return cleared;
        return { ...cleared, current_step: STEP_ORDER[idx + 1] };
      });
    },
    goBack() {
      store.setState((prev) => {
        const idx = indexOf(prev.current_step);
        if (idx === 0) return prev;
        return { ...prev, current_step: STEP_ORDER[idx - 1] };
      });
    },
    skipCurrent() {
      store.setState((prev) => {
        if (!SKIPPABLE_STEPS.has(prev.current_step)) {
          throw new Error(`Step ${prev.current_step} cannot be skipped`);
        }
        const cleared = withSkipped(prev, prev.current_step);
        const idx = indexOf(prev.current_step);
        if (idx + 1 >= STEP_ORDER.length) return cleared;
        return { ...cleared, current_step: STEP_ORDER[idx + 1] };
      });
    },
    jumpTo(step: StepId) {
      store.setState((prev) => ({ ...prev, current_step: step }));
    },
    setWelcome(patch: Partial<WelcomePayload>) {
      store.setState((prev) => ({
        ...prev,
        welcome: { ...prev.welcome, ...patch },
      }));
    },
    setStorage(payload: StoragePayload | null) {
      store.setState((prev) => ({ ...prev, storage: payload }));
    },
    setLlmProvider(payload: LlmProviderPayload | null) {
      store.setState((prev) => ({ ...prev, llm_provider: payload }));
    },
    setFirstProject(payload: FirstProjectPayload | null) {
      store.setState((prev) => ({ ...prev, first_project: payload }));
    },
    finalize() {
      store.setState((prev) => {
        const cleared = withCompleted(prev, prev.current_step);
        return { ...cleared, completed_at: new Date().toISOString() };
      });
    },
    reset() {
      store.reset();
    },
  };
}

export const wizardActions = buildActions(wizardStore);

export function stepStatus(
  state: WizardState,
  step: StepId
): "active" | "completed" | "skipped" | "pending" {
  if (state.completed_steps.includes(step)) return "completed";
  if (state.skipped_steps.includes(step)) return "skipped";
  if (state.current_step === step && !state.completed_at) return "active";
  return "pending";
}

// ---------------------------------------------------------------------------
// React hook (App Router safe)
// ---------------------------------------------------------------------------

export function useWizardStore(): WizardState {
  return useSyncExternalStore(
    wizardStore.subscribe,
    wizardStore.getState,
    wizardStore.getState
  );
}

export function useHydrated(): boolean {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setHydrated(true);
  }, []);
  return hydrated;
}
