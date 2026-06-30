// v1.0.0 - 2026-03-13 - Activity log page with cursor-based pagination
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { APIError, getAuditLog } from "@/lib/api";
import type { AuditLogEntry } from "@/lib/types";
import ActivityLogTable from "@/components/settings/ActivityLogTable";
import ActivityLogFilters from "@/components/settings/ActivityLogFilters";

const PAGE_SIZE = 200;
const MAX_LOADED = 1000;

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function activityErrorMessage(err: unknown): string {
  if (err instanceof APIError && err.status === 403) {
    return "Admin access is required to view the audit log.";
  }
  return err instanceof Error ? err.message : "Failed to load activity log";
}

function ActivityFooter({
  loading,
  count,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  loading: boolean;
  count: number;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  if (loading || count === 0) return null;

  if (count >= MAX_LOADED) {
    return (
      <div className="flex justify-center">
        <span className="text-xs text-pir-text-muted">
          Showing {count} events. Export to CSV for full history.
        </span>
      </div>
    );
  }

  if (!hasMore) {
    return (
      <div className="flex justify-center">
        <span className="text-xs text-pir-text-muted">All events loaded</span>
      </div>
    );
  }

  return (
    <div className="flex justify-center">
      <button
        onClick={onLoadMore}
        disabled={loadingMore}
        className="px-4 py-2 bg-pir-surface-0 border border-pir rounded text-xs text-pir-text-secondary hover:bg-pir-surface-1 disabled:opacity-50 transition-colors"
      >
        {loadingMore ? "Loading..." : "Load more"}
      </button>
    </div>
  );
}

export default function ActivityPage() {
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [action, setAction] = useState("");

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
          action: action || undefined,
          offset: reset || cursor === null ? 0 : Number(cursor),
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
      if (isAbortError(err)) return;
      setError(activityErrorMessage(err));
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [action, cursor]);

  // Reset on filter change
  useEffect(() => {
    fetchEntries(true);
    return () => { abortRef.current?.abort(); };
  }, [action]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="p-6 max-w-4xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-heading text-pir-text-primary">Activity Log</h1>
        <ActivityLogFilters
          action={action}
          onActionChange={(v) => setAction(v)}
        />
      </div>

      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-3 py-2 text-xs text-red-700 dark:text-red-400 flex items-center justify-between">
          <span>{error}</span>
          <button onClick={() => fetchEntries(true)} className="text-pir-accent hover:underline">Retry</button>
        </div>
      )}

      {!error && (
        <div className="bg-pir-surface-0 border border-pir rounded-lg overflow-hidden">
          <ActivityLogTable entries={entries} loading={loading} />
        </div>
      )}

      {!error && (
        <ActivityFooter
          loading={loading}
          count={entries.length}
          hasMore={hasMore}
          loadingMore={loadingMore}
          onLoadMore={() => fetchEntries(false)}
        />
      )}
    </div>
  );
}
