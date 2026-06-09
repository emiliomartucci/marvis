// v1.0.0 - 2026-05-17 - Hook persistenza view state Codex (LS marvisx.codex.v1).
//
// Mirror di cosmo/useGraphViewState ma con namespace LS dedicato: Codex
// persiste zoom/pan + posizioni modulo (drag Alt+click). Stesso pattern di
// safeParse + sanitize + debounce 250ms.

"use client";

import { useCallback, useEffect, useState } from "react";
import { z } from "zod";

const LS_KEY = "marvisx.codex.v1";
const LS_DEBOUNCE_MS = 250;
const MAX_NODE_OVERRIDES = 500;
const PROTO_KEYS = new Set(["__proto__", "constructor", "prototype"]);

const OverrideZ = z.object({
  x: z.number().finite(),
  y: z.number().finite(),
});
/** @public */
export type CodexOverride = z.infer<typeof OverrideZ>;

const CodexViewStateZ = z.object({
  zoom: z.number().min(0.05).max(100),
  pan: z.object({
    x: z.number().finite(),
    y: z.number().finite(),
  }),
  nodeOverrides: z.record(z.string(), OverrideZ),
});
/** @public */
export type CodexViewState = z.infer<typeof CodexViewStateZ>;

const DEFAULT_VIEW_STATE: CodexViewState = {
  zoom: 0.7,
  pan: { x: 0, y: 0 },
  nodeOverrides: {},
};

function sanitize(state: CodexViewState): CodexViewState {
  const safe: Record<string, CodexOverride> = Object.create(null) as Record<
    string,
    CodexOverride
  >;
  let count = 0;
  for (const [k, v] of Object.entries(state.nodeOverrides)) {
    if (PROTO_KEYS.has(k)) continue;
    if (count >= MAX_NODE_OVERRIDES) break;
    safe[k] = v;
    count += 1;
  }
  return { zoom: state.zoom, pan: state.pan, nodeOverrides: safe };
}

function loadFromLS(): CodexViewState {
  if (typeof window === "undefined") return DEFAULT_VIEW_STATE;
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return DEFAULT_VIEW_STATE;
    const json: unknown = JSON.parse(raw);
    const parsed = CodexViewStateZ.safeParse(json);
    if (!parsed.success) {
      console.warn(
        "[codex] LS state invalid, resetting",
        parsed.error.flatten(),
      );
      return DEFAULT_VIEW_STATE;
    }
    return sanitize(parsed.data);
  } catch (err) {
    console.warn("[codex] LS state unreadable, resetting", err);
    return DEFAULT_VIEW_STATE;
  }
}

/**
 * Hook view state persistente Codex. Ritorna `[state, patch]` stabile +
 * action helpers `setOverride` / `clearOverrides`.
 * @public
 */
export function useCodexViewState() {
  const [state, setState] = useState<CodexViewState>(loadFromLS);

  const patch = useCallback((next: Partial<CodexViewState>) => {
    setState((prev) => ({ ...prev, ...next }));
  }, []);

  const setOverride = useCallback((slug: string, pos: CodexOverride) => {
    setState((prev) => {
      const overrides = { ...prev.nodeOverrides, [slug]: pos };
      return { ...prev, nodeOverrides: overrides };
    });
  }, []);

  const clearOverrides = useCallback(() => {
    setState((prev) => ({ ...prev, nodeOverrides: {} }));
  }, []);

  // Debounce scrittura LS — evita thrash durante pan/zoom continuo o drag.
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

  return { state, patch, setOverride, clearOverrides } as const;
}
