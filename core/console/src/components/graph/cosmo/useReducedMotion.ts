// v1.0.0 - 2026-04-24 - Hook `prefers-reduced-motion` (D-08 piano).
//
// Usa `useSyncExternalStore` → zero tearing, reattivo a cambio preferenza
// utente. Gate per beautify tween + animateView (reduced = swap istantaneo).
"use client";

import { useSyncExternalStore } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return () => undefined;
  }
  const mql = window.matchMedia(QUERY);
  // Safari <14 usa addListener/removeListener; i moderni addEventListener.
  if (typeof mql.addEventListener === "function") {
    mql.addEventListener("change", callback);
    return () => mql.removeEventListener("change", callback);
  }
  // Safari legacy: tipi DOM non includono piu' addListener/removeListener.
  // Caster esplicito per evitare @ts-expect-error che Next.js type-check rifiuta.
  type LegacyMql = {
    addListener: (cb: () => void) => void;
    removeListener: (cb: () => void) => void;
  };
  const legacy = mql as unknown as LegacyMql;
  legacy.addListener(callback);
  return () => legacy.removeListener(callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(QUERY).matches;
}

function getServerSnapshot(): boolean {
  return false;
}

/**
 * Ritorna `true` se l'utente ha chiesto reduced motion. SSR-safe.
 * @public
 */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
