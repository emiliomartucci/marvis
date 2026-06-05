"use client";

import { useSyncExternalStore } from "react";

// Design System v2 flag. Persisted in localStorage, mirrored as a class
// on <html>. Pre-hydration script in src/app/layout.tsx applies the class
// before first paint to avoid FOUC. v2 is default-on; explicit "false" opts out.
const STORAGE_KEY = "marvisx:design-v2";
const CHANGE_EVENT = "marvisx:design-v2-change";
let memoryDesignV2: boolean | null = null;

function subscribe(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener(CHANGE_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(CHANGE_EVENT, callback);
  };
}

function getSnapshot(): boolean {
  try {
    const storedValue = localStorage.getItem(STORAGE_KEY);
    return storedValue === null ? true : storedValue !== "false";
  } catch {
    return memoryDesignV2 ??
      (typeof document === "undefined" ? true : document.documentElement.classList.contains("theme-v2"));
  }
}

function getServerSnapshot(): boolean {
  return true;
}

export function useDesignV2(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

export function setDesignV2(enabled: boolean) {
  memoryDesignV2 = enabled;
  try {
    localStorage.setItem(STORAGE_KEY, enabled ? "true" : "false");
  } catch {
    // Safari private mode, etc. — apply the class in-memory anyway so the
    // toggle still works within the current session.
  }
  document.documentElement.classList.toggle("theme-v2", enabled);
  window.dispatchEvent(new Event(CHANGE_EVENT));
}
