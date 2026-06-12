"use client";

import { useCallback, useEffect, useState } from "react";
import { en } from "./en";
import { it } from "./it";
import type { Locale, TranslationDictionary } from "./shared";

const STORAGE_KEY = "marvis:locale";
const LOCALE_CHANGED_EVENT = "marvis:locale-changed";
const dictionaries: Record<Locale, TranslationDictionary> = { en, it };

function resolveLocale(value: string | null | undefined): Locale {
  const normalized = value?.toLowerCase() ?? "";
  if (normalized.startsWith("it")) return "it";
  if (normalized.startsWith("en")) return "en";
  return "en";
}

function readInitialLocale(): Locale {
  if (typeof window === "undefined") return "en";
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return resolveLocale(stored || window.navigator.language);
  } catch {
    return resolveLocale(window.navigator.language);
  }
}

export function useT(): {
  t: TranslationDictionary;
  locale: Locale;
  setLocale: (locale: Locale) => void;
} {
  const [locale, setLocaleState] = useState<Locale>(() => readInitialLocale());

  useEffect(() => {
    function syncLocale() {
      setLocaleState(readInitialLocale());
    }
    window.addEventListener(LOCALE_CHANGED_EVENT, syncLocale);
    window.addEventListener("storage", syncLocale);
    return () => {
      window.removeEventListener(LOCALE_CHANGED_EVENT, syncLocale);
      window.removeEventListener("storage", syncLocale);
    };
  }, []);

  const setLocale = useCallback((nextLocale: Locale) => {
    setLocaleState(nextLocale);
    try {
      window.localStorage.setItem(STORAGE_KEY, nextLocale);
    } catch {
      // Read-only storage should not break the static Console.
    }
    window.dispatchEvent(new Event(LOCALE_CHANGED_EVENT));
  }, []);

  return { t: dictionaries[locale], locale, setLocale };
}
