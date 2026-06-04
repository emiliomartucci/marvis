// v1.0.0 - 2026-03-13 - Activity log page with cursor-based pagination
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getAuditLog } from "@/lib/api";
import type { AuditLogEntry } from "@/lib/types";
import ActivityLogTable from "@/components/settings/ActivityLogTable";
import ActivityLogFilters from "@/components/settings/ActivityLogFilters";

const PAGE_SIZE = 200;
const MAX_LOADED = 1000;

export default function ActivityPage() {
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [eventType, setEventType] = useState("");
  const [period, setPeriod] = useState(7);

  const abortRef = useRef<AbortController | null>(null);

  const fetchEntries = useCallback(async (reset = false) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    if (reset) {
      setLoading(true);
      setEntries([]);
      setCursor(null);
    } else {
      setLoadingMore(true);
    }
    setError(null);

    try {
      const response = await getAuditLog(
        {
          event_type: eventType || undefined,
          period,
          cursor: reset ? undefined : cursor ?? undefined,
          limit: PAGE_SIZE,
        },
        { signal: controller.signal }
      );

      if (reset) {
        setEntries(response.entries);
      } else {
        setEntries((prev) => [...prev, ...response.entries]);
      }
      setCursor(response.next_cursor);
      setHasMore(!!response.next_cursor);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setError(err instanceof Error ? err.message : "Failed to load activity log");
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [eventType, period, cursor]);

  // Reset on filter change
  useEffect(() => {
    fetchEntries(true);
    return () => { abortRef.current?.abort(); };
  }, [eventType, period]); // eslint-disable-line react-hooks/exhaustive-deps

  const reachedMax = entries.length >= MAX_LOADED;

  return (
    <div className="p-6 max-w-4xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-heading text-pir-text-primary">Activity Log</h1>
        <ActivityLogFilters
          eventType={eventType}
          period={period}
          onEventTypeChange={(v) => setEventType(v)}
          onPeriodChange={(v) => setPeriod(v)}
        />
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2 text-xs text-red-700 dark:text-red-400 flex items-center justify-between">
          <span>{error}</span>
          <button onClick={() => fetchEntries(true)} className="text-pir-accent hover:underline">Retry</button>
        </div>
      )}

      <div className="bg-pir-surface-0 border border-pir rounded-lg overflow-hidden">
        <ActivityLogTable entries={entries} loading={loading} />
      </div>

      {/* Load more / export */}
      {!loading && entries.length > 0 && (
        <div className="flex justify-center">
          {reachedMax ? (
            <span className="text-xs text-pir-text-muted">
              Showing {entries.length} events. Export to CSV for full history.
            </span>
          ) : hasMore ? (
            <button
              onClick={() => fetchEntries(false)}
              disabled={loadingMore}
              className="px-4 py-2 bg-pir-surface-0 border border-pir rounded text-xs text-pir-text-secondary hover:bg-pir-surface-1 disabled:opacity-50 transition-colors"
            >
              {loadingMore ? "Loading..." : "Load more"}
            </button>
          ) : (
            <span className="text-xs text-pir-text-muted">All events loaded</span>
          )}
        </div>
      )}
    </div>
  );
}
