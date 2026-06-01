// v1.1.0 - 2026-04-27 - PR #22: aggrega project slugs da TUTTI i doc_type (era solo
//                       result.projects); threshold abbassato 0.6→0.4 (FTS scores live
//                       gravitano 0.4-0.5; >0.6 quasi mai matchato).
// v1.0.0 - 2026-04-27 - PR #21: hook semantic search → Set<projectSlug> per highlight canvas Cosmo.
//
// Debounce 300ms su `query`, abort della richiesta in volo se la query cambia
// prima del settle. 503/timeout/abort → set vuoto best-effort (no UI error:
// la search e' optional UX, non blocking).
"use client";

import { useEffect, useState } from "react";
import { globalSearch, type SearchHit, type SearchResponse } from "@/lib/api";

const MIN_QUERY_LEN = 2;
const DEBOUNCE_MS = 300;
const SCORE_THRESHOLD = 0.4;

/** Doc_type che portano un campo `project` semanticamente uguale al slug del
 * progetto su graph canvas. tasks/projects/files/handoffs coperti dal SearchHit
 * type. learnings/audits/inbox_items live nel BE ma non ancora typati nel
 * SearchResponse FE (extension future) — quando aggiunti includere qui. */
const SLUG_FIELDS: ReadonlyArray<keyof SearchResponse> = [
  "tasks",
  "projects",
  "files",
  "handoffs",
];

/**
 * Restituisce un `Set<string>` di project slugs che matchano `query`. Aggrega
 * project slugs da TUTTI i doc_type (task/file/handoff/learning), non solo
 * `result.projects`: se un task del project "cer" matcha la query, "cer"
 * viene highlightato sul canvas. Threshold 0.4 (FTS scores live medi).
 *
 * Re-fire ad ogni cambio di `query`. Cleanup cancella sia il timer di debounce
 * sia il fetch in volo (AbortController) → no race su query rapidamente
 * sostituite.
 */
export function useGraphSearch(query: string): Set<string> {
  const [matches, setMatches] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < MIN_QUERY_LEN) {
      setMatches((prev) => (prev.size === 0 ? prev : new Set()));
      return undefined;
    }

    const ctrl = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const result = await globalSearch(trimmed, { signal: ctrl.signal });
        if (ctrl.signal.aborted) return;
        const slugs = new Set<string>();
        for (const field of SLUG_FIELDS) {
          const hits = (result[field] ?? []) as SearchHit[];
          for (const h of hits) {
            if ((h.score ?? 0) <= SCORE_THRESHOLD) continue;
            const slug = h.project;
            if (typeof slug === "string" && slug.length > 0) {
              slugs.add(slug);
            }
          }
        }
        setMatches(slugs);
      } catch {
        if (!ctrl.signal.aborted) {
          setMatches((prev) => (prev.size === 0 ? prev : new Set()));
        }
      }
    }, DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [query]);

  return matches;
}
