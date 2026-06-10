// v1.0.0 - 2026-04-24 - Hook persistenza view state Cosmo (LS `marvisx.graph.v1`).
//
// API [state, patch] (H-01 piano): swap a BE futuro e' diff di 10 linee.
// safeParse + console.warn su LS invalido (M-FE-10), sanitizza `__proto__` +
// cap 500 entries in nodeOverrides (M-FE-11), write debounced 250ms (M-FE-03).
"use client";

import { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_VIEW_STATE,
  ViewStateZ,
  type ViewState,
  type Override,
} from "./types";

const LS_KEY = "marvisx.graph.v1";
const LS_DEBOUNCE_MS = 250;
const MAX_NODE_OVERRIDES = 500;
const PROTO_KEYS = new Set(["__proto__", "constructor", "prototype"]);

/** Load + sanitize view state da LS. Ritorna DEFAULT su qualunque errore. */
function loadFromLS(): ViewState {
  if (typeof window === "undefined") return DEFAULT_VIEW_STATE;
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return DEFAULT_VIEW_STATE;
    const json: unknown = JSON.parse(raw);
    const parsed = ViewStateZ.safeParse(json);
    if (!parsed.success) {
      console.warn(
        "[graph] LS state invalid, resetting",
        parsed.error.flatten(),
      );
      return DEFAULT_VIEW_STATE;
    }
    return sanitize(parsed.data);
  } catch (err) {
    console.warn("[graph] LS state unreadable, resetting", err);
    return DEFAULT_VIEW_STATE;
  }
}

/** Rimuove chiavi pollution-risk e cappa nodeOverrides a 500 entries. */
function sanitize(state: ViewState): ViewState {
  const safeOverrides: Record<string, Override> = Object.create(null) as Record<
    string,
    Override
  >;
  let count = 0;
  for (const [k, v] of Object.entries(state.nodeOverrides)) {
    if (PROTO_KEYS.has(k)) continue;
    if (count >= MAX_NODE_OVERRIDES) break;
    safeOverrides[k] = v;
    count += 1;
  }
  return { zoom: state.zoom, pan: state.pan, nodeOverrides: safeOverrides };
}

/**
 * Hook view state persistente. Ritorna tuple `[state, patch]` stable.
 * @public — consumato da GraphCanvas + GraphPage.
 */
export function useGraphViewState() {
  const [state, setState] = useState<ViewState>(loadFromLS);

  const patch = useCallback((next: Partial<ViewState>) => {
    setState((prev) => ({ ...prev, ...next }));
  }, []);

  // Debounce scrittura LS: evita thrash su pan/zoom continuo.
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const id = window.setTimeout(() => {
      try {
        window.localStorage.setItem(LS_KEY, JSON.stringify(state));
      } catch {
        /* privacy mode o quota: niente persistenza, state in-memory */
      }
    }, LS_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [state]);

  return [state, patch] as const;
}

// Esportati per testabilita'.
/** @public — esposto per i test di `useGraphViewState`. */
export const __test__ = {
  LS_KEY,
  LS_DEBOUNCE_MS,
  MAX_NODE_OVERRIDES,
  loadFromLS,
  sanitize,
};
