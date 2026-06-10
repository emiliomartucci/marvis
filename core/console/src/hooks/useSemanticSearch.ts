"use client";

import { useState, useRef, useCallback } from "react";
import { globalSearch, type SearchHit, type SearchResponse } from "@/lib/api";

export function useSemanticSearch(debounceMs = 300) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const search = useCallback(
    (value: string) => {
      setQuery(value);

      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (abortRef.current) abortRef.current.abort();

      if (!value.trim()) {
        setResults(null);
        setLoading(false);
        setError(null);
        return;
      }

      setLoading(true);
      setError(null);

      debounceRef.current = setTimeout(async () => {
        const controller = new AbortController();
        abortRef.current = controller;

        let canceled = false;
        controller.signal.addEventListener("abort", () => {
          canceled = true;
        });

        try {
          const data = await globalSearch(value.trim(), {
            signal: controller.signal,
          });
          if (!canceled) {
            setResults(data);
            setLoading(false);
          }
        } catch (err) {
          if (!canceled) {
            if ((err as Error).name !== "AbortError") {
              setError("Search unavailable");
            }
            setLoading(false);
          }
        }
      }, debounceMs);
    },
    [debounceMs],
  );

  const clear = useCallback(() => {
    setQuery("");
    setResults(null);
    setLoading(false);
    setError(null);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (abortRef.current) abortRef.current.abort();
  }, []);

  const allHits: SearchHit[] = results
    ? [
        ...results.tasks,
        ...results.projects,
        ...results.files,
        ...results.handoffs,
      ]
    : [];

  return { query, search, clear, results, allHits, loading, error };
}
