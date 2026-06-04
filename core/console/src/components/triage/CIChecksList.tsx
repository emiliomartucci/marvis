// v1.0.0 - 2026-03-13 - Expandable CI checks list with polling
"use client";

import { useCallback, useState } from "react";
import { getCIChecks, getCIChecksSummary } from "@/lib/api";
import { usePollingData } from "@/hooks/usePollingData";
import CIStatusBadge from "./CIStatusBadge";
import type { CICheck, CIChecksSummary } from "@/lib/types";

interface CIChecksListProps {
  taskId: string;
  hasPr: boolean;
}

export default function CIChecksList({ taskId, hasPr }: CIChecksListProps) {
  const [expanded, setExpanded] = useState(false);

  const fetchSummary = useCallback(
    (signal: AbortSignal) => getCIChecksSummary(taskId, { signal }),
    [taskId]
  );

  const fetchChecks = useCallback(
    (signal: AbortSignal) => getCIChecks(taskId, { signal }),
    [taskId]
  );

  const { data: summary, loading: summaryLoading, error: summaryError, refresh: refreshSummary } =
    usePollingData<CIChecksSummary>(fetchSummary, {
      interval: 15_000,
      enabled: hasPr,
      backoff: true,
      unchangedThreshold: 3,
    });

  const hasPending = summary ? summary.pending > 0 : false;

  const { data: checks, loading: checksLoading } = usePollingData<CICheck[]>(fetchChecks, {
    interval: 15_000,
    enabled: hasPr && expanded && hasPending,
    backoff: true,
  });

  if (!hasPr) return null;

  if (summaryLoading && !summary) {
    return (
      <div className="animate-pulse h-6 bg-pir-surface-0 rounded w-24" />
    );
  }

  if (summaryError) {
    return (
      <div className="flex items-center gap-2 text-xs text-pir-text-muted">
        <span>Unable to load CI status</span>
        <button
          onClick={refreshSummary}
          className="text-pir-accent hover:underline"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!summary) return null;

  return (
    <div className="space-y-2">
      {/* Merge blocked banner */}
      {summary.merge_blocked && (
        <div className="flex items-center gap-1.5 text-xs text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded px-2.5 py-1.5">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="shrink-0">
            <path d="M6 1L11 10H1L6 1Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
            <path d="M6 4.5V6.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            <circle cx="6" cy="8" r="0.5" fill="currentColor" />
          </svg>
          Merge blocked — {summary.required_failing.length} required check{summary.required_failing.length !== 1 ? "s" : ""} failing
        </div>
      )}

      {/* Badge + expand toggle */}
      <div className="flex items-center gap-2">
        <CIStatusBadge summary={summary} onClick={() => setExpanded(!expanded)} />
        {summary.total > 0 && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[10px] text-pir-text-muted hover:text-pir-text-secondary"
          >
            {expanded ? "Hide checks" : `${summary.total} check${summary.total !== 1 ? "s" : ""}`}
          </button>
        )}
      </div>

      {/* Expanded check list */}
      {expanded && (
        <div className="border border-pir rounded overflow-hidden">
          {checksLoading && !checks ? (
            <div className="p-2 space-y-1.5">
              {[1, 2, 3].map((i) => (
                <div key={i} className="animate-pulse h-5 bg-pir-surface-0 rounded" />
              ))}
            </div>
          ) : checks && checks.length > 0 ? (
            <div className="max-h-48 overflow-y-auto">
              {checks.map((check) => (
                <CheckRow key={check.id} check={check} />
              ))}
            </div>
          ) : (
            <div className="p-2 text-xs text-pir-text-muted">No checks found</div>
          )}
        </div>
      )}
    </div>
  );
}

function CheckRow({ check }: { check: CICheck }) {
  const icon = checkStatusIcon(check);
  const duration = check.started_at && check.completed_at
    ? formatDuration(new Date(check.completed_at).getTime() - new Date(check.started_at).getTime())
    : null;

  // Sanitize check name (display as text, not HTML)
  const safeName = check.check_name;

  return (
    <div className="flex items-center gap-2 px-2.5 py-1.5 text-xs border-b border-pir last:border-b-0 hover:bg-pir-surface-1/50">
      {icon}
      <span className="flex-1 min-w-0 truncate text-pir-text-secondary">{safeName}</span>
      {duration && (
        <span className="text-[10px] text-pir-text-muted shrink-0">{duration}</span>
      )}
      {check.details_url && (
        <a
          href={check.details_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-pir-accent hover:underline shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          Details
        </a>
      )}
    </div>
  );
}

function checkStatusIcon(check: CICheck): React.ReactNode {
  if (check.status === "in_progress" || check.status === "queued") {
    return (
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="shrink-0 text-amber-500 animate-spin">
        <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.2" opacity="0.3" />
        <path d="M6 2a4 4 0 012.83 1.17" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
      </svg>
    );
  }

  if (check.conclusion === "success") {
    return (
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="shrink-0 text-emerald-500">
        <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (check.conclusion === "failure") {
    return (
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="shrink-0 text-red-500">
        <path d="M3 3L9 9M9 3L3 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    );
  }

  if (check.conclusion === "skipped" || check.conclusion === "neutral") {
    return (
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="shrink-0 text-gray-400">
        <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.2" />
        <path d="M4 6H8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
      </svg>
    );
  }

  // cancelled, timed_out, action_required
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="shrink-0 text-gray-400">
      <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remainSeconds}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}
