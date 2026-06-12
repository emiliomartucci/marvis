// Brain UX — small in-memory cache that translates BLAKE2b event_ids into
// real titles fetched from /api/v1/brain/events. Used by Diario,
// Memoria, Da-decidere to show "Decisione · feat(brain): split..." instead
// of "Decisione · abc12345…".

"use client";

import { useEffect, useState } from "react";

import { fetchEvents } from "@/lib/brain/surfaces";

interface EventMeta {
  event_id: string;
  title: string | null;
  source_system: string | null;
  source_project: string | null;
  event_type: string | null;
}

/** Resolve a set of event_ids to their titles. Stable cache per cycle. */
export function useEventTitles(
  eventIds: string[],
  cycleKey: string | null,
): Record<string, EventMeta> {
  const [cache, setCache] = useState<Record<string, EventMeta>>({});

  useEffect(() => {
    if (!eventIds.length || !cycleKey) return;
    const missing = eventIds.filter((id) => id && !(id in cache));
    if (missing.length === 0) return;

    let active = true;
    (async () => {
      try {
        // Cap batch — endpoint paginates, but the daily UI rarely needs > 50.
        const ids = missing.slice(0, 100);
        const resp = await fetchEvents({
          cycle_key: cycleKey,
          limit: 200,
        });
        if (!active) return;
        const idSet = new Set(ids);
        const next: Record<string, EventMeta> = { ...cache };
        for (const ev of resp.items as unknown as Array<Record<string, unknown>>) {
          const evId = ev.event_id as string | undefined;
          if (!evId) continue;
          if (!idSet.has(evId)) continue;
          next[evId] = {
            event_id: evId,
            title: (ev.title as string | null | undefined) ?? null,
            source_system: (ev.source_system as string | null | undefined) ?? null,
            source_project: (ev.source_project as string | null | undefined) ?? null,
            event_type: (ev.event_type as string | null | undefined) ?? null,
          };
        }
        setCache(next);
      } catch {
        // Silent failure — fallback to short event_id render.
      }
    })();
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cycleKey, eventIds.join(",")]);

  return cache;
}

export type { EventMeta };
