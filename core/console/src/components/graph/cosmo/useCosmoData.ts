// v1.0.0 - 2026-04-24 - Hook React per fetch `/graph/cosmo` con abort + timeout
//
// M-FE-05 piano: AbortController, non flag `cancelled`. Cleanup effect →
// ctrl.abort() garantisce che una response in-flight venga scartata se il
// componente smonta prima del resolve.
//
// H-06 piano: timeout 10s — i canvas con 80+ project stressano BE se il
// populator e' in corso; 10s e' il cap oltre cui e' meglio mostrare error.
"use client";

import { useEffect, useState } from "react";
import { getGraphCosmo } from "@/lib/api";
import type { Edge, Project } from "./types";

const FETCH_TIMEOUT_MS = 10_000;

export interface CosmoState {
  data: { projects: Project[]; edges: Edge[] } | null;
  error: string | null;
  loading: boolean;
}

const INITIAL: CosmoState = { data: null, error: null, loading: true };

export function useCosmoData(): CosmoState {
  const [state, setState] = useState<CosmoState>(INITIAL);

  useEffect(() => {
    const ctrl = new AbortController();
    // Timeout dedicato: abort() con reason 'timeout' se BE non risponde.
    const timeoutId = window.setTimeout(() => ctrl.abort("timeout"), FETCH_TIMEOUT_MS);

    // NB: `loading: true` e' gia' nello state iniziale (INITIAL). Non reinit
    // in effect per non triggerare il lint `no-initialize-state`.

    getGraphCosmo({ signal: ctrl.signal })
      .then((d) => {
        setState({ data: d, error: null, loading: false });
      })
      .catch((e: unknown) => {
        // AbortError atteso durante cleanup/timeout → non sovrascrivere
        // lo state con un errore utente-visible.
        if (e instanceof Error && e.name === "AbortError") return;
        const msg = e instanceof Error ? e.message : String(e);
        setState({ data: null, error: msg, loading: false });
      })
      .finally(() => {
        window.clearTimeout(timeoutId);
      });

    return () => {
      window.clearTimeout(timeoutId);
      ctrl.abort();
    };
  }, []);

  return state;
}
