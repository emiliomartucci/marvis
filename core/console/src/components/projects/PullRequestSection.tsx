// v1.3.0 - 2026-04-22 - Resilient error handling: retry button for transient network failures
"use client";

import { useCallback, useEffect, useState } from "react";
import { getPullRequest, mergePullRequest, closePullRequest, approvePR, requestPRChanges } from "@/lib/api";
import type { PullRequest, PrStatus, UserInfo } from "@/lib/types";
import { PermissionGate } from "@/components/PermissionGate";
import DiffViewer from "./DiffViewer";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

const PR_STATUS_BADGE: Record<PrStatus, { label: string; cls: string }> = {
  draft: { label: "Branch", cls: "bg-pir-text-muted/20 text-pir-text-muted" },
  open: { label: "In Review", cls: "bg-pir-warning/20 text-pir-warning" },
  merging: { label: "Merging...", cls: "bg-pir-accent/20 text-pir-accent" },
  merged: { label: "Merged", cls: "bg-pir-success/20 text-pir-success" },
  closed: { label: "Closed", cls: "bg-pir-error/20 text-pir-error" },
};

interface Props {
  taskId: string;
  onTaskCompleted?: () => void;
  currentUser?: UserInfo | null;
}

export default function PullRequestSection({ taskId, onTaskCompleted, currentUser }: Props) {
  const [pr, setPr] = useState<PullRequest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryKey, setRetryKey] = useState(0);

  // Merge state
  const [merging, setMerging] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [conflictFiles, setConflictFiles] = useState<string[] | null>(null);

  // Close state
  const [showCloseModal, setShowCloseModal] = useState(false);
  const [closeReason, setCloseReason] = useState("");
  const [closing, setClosing] = useState(false);

  // Approve state
  const [approving, setApproving] = useState(false);

  // Request changes state
  const [showRequestChanges, setShowRequestChanges] = useState(false);
  const [requestChangesComment, setRequestChangesComment] = useState("");
  const [requestingChanges, setRequestingChanges] = useState(false);

  // Diff expand
  const [showDiff, setShowDiff] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    getPullRequest(taskId, { signal: controller.signal })
      .then((data) => setPr(data))
      .catch((err) => {
        if (err.name !== "AbortError") {
          // 404 means no PR exists for this task — not an error
          if (err.message?.includes("404")) {
            setPr(null);
          } else {
            setError(err.message ?? "Unknown error");
          }
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [taskId, retryKey]);

  const handleMerge = useCallback(async () => {
    if (!pr) return;
    setMerging(true);
    setMergeError(null);
    setConflictFiles(null);

    try {
      await mergePullRequest(taskId);
      setPr((prev) => (prev ? { ...prev, status: "merged" } : prev));
      onTaskCompleted?.();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Merge failed";
      // Check for conflict response
      try {
        const parsed = JSON.parse(msg);
        if (parsed.conflicting_files) {
          setConflictFiles(parsed.conflicting_files);
          setMergeError("Merge conflict");
          return;
        }
      } catch {
        // Not JSON, regular error
      }
      setMergeError(msg);
    } finally {
      setMerging(false);
    }
  }, [pr, taskId, onTaskCompleted]);

  const handleClose = useCallback(async () => {
    setClosing(true);
    try {
      await closePullRequest(taskId, closeReason);
      setPr((prev) =>
        prev ? { ...prev, status: "closed", closed_reason: closeReason || null } : prev
      );
      setShowCloseModal(false);
      setCloseReason("");
    } catch (err) {
      setMergeError(err instanceof Error ? err.message : "Close failed");
    } finally {
      setClosing(false);
    }
  }, [taskId, closeReason]);

  const handleApprove = useCallback(async () => {
    if (!pr) return;
    setApproving(true);
    setMergeError(null);
    try {
      const updated = await approvePR(taskId);
      setPr(updated);
    } catch (err) {
      setMergeError(err instanceof Error ? err.message : "Approve failed");
    } finally {
      setApproving(false);
    }
  }, [pr, taskId]);

  const handleRequestChanges = useCallback(async () => {
    setRequestingChanges(true);
    try {
      await requestPRChanges(taskId, requestChangesComment);
      setPr((prev) => (prev ? { ...prev, approved_by: null, approved_at: null } : prev));
      setShowRequestChanges(false);
      setRequestChangesComment("");
    } catch (err) {
      setMergeError(err instanceof Error ? err.message : "Request changes failed");
    } finally {
      setRequestingChanges(false);
    }
  }, [taskId, requestChangesComment]);

  if (loading) {
    return (
      <div className="text-caption text-pir-text-muted py-2">Loading PR...</div>
    );
  }

  // No PR for this task — don't render anything
  if (!pr && !error) return null;

  if (error) {
    // Transient network error — don't alarm the user, offer a quiet retry
    if (error === "Failed to fetch") {
      return (
        <div className="text-caption text-pir-text-muted py-2 flex items-center gap-2">
          <span>PR data unavailable</span>
          <button
            onClick={() => setRetryKey((k) => k + 1)}
            className="text-pir-accent hover:text-pir-accent/80 transition-colors underline-offset-2 hover:underline"
          >
            Retry
          </button>
        </div>
      );
    }
    return (
      <ErrorAlert message={`PR error: ${error}`} className="py-2" />
    );
  }

  if (!pr) return null;

  const badge = PR_STATUS_BADGE[pr.status];
  const canMerge = pr.status === "open";
  const canClose = pr.status === "draft" || pr.status === "open";
  const isTerminal = pr.status === "merged" || pr.status === "closed";

  // Four-eyes gate: admin/super_admin, not the submitter, PR is open
  const canReview =
    (currentUser?.system_role === "admin" || currentUser?.system_role === "super_admin") &&
    pr.submitted_by !== currentUser?.user_id &&
    pr.status === "open";

  return (
    <div className="border border-pir rounded bg-pir-surface-0">
      {/* Header */}
      <div className="px-3 py-2 flex items-center gap-2 border-b border-pir">
        <span className="text-caption font-mono text-pir-text-muted truncate flex-1">
          {pr.branch}
        </span>
        <span className={`text-[10px] px-2 py-0.5 rounded-full ${badge.cls}`}>
          {badge.label}
        </span>
        {/* View in Graph — feature flagged */}
        {process.env.NEXT_PUBLIC_ENABLE_GRAPH_UX === "true" && (
          <a
            href={`/graph/?id=pr:artifact:${taskId}&tab=context`}
            title="View PR in Knowledge Graph"
            className="text-[10px] text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          >
            KG →
          </a>
        )}
      </div>

      {/* Title + stats */}
      <div className="px-3 py-2 space-y-1">
        {pr.title && (
          <div className="text-xs text-pir-text-primary font-medium">{pr.title}</div>
        )}
        {pr.diff && (
          <div className="flex items-center gap-3 text-caption text-pir-text-muted">
            <span>{pr.diff.stats.files_changed} file{pr.diff.stats.files_changed !== 1 ? "s" : ""}</span>
            <span className="text-pir-success">+{pr.diff.stats.additions}</span>
            <span className="text-pir-error">-{pr.diff.stats.deletions}</span>
            {pr.diff.unified_diff && (
              <button
                onClick={() => setShowDiff((v) => !v)}
                className="text-pir-accent hover:text-pir-accent/80 transition-colors ml-auto"
              >
                {showDiff ? "Hide diff" : "Show diff"}
              </button>
            )}
          </div>
        )}
      </div>

      {/* Diff viewer (expandable) */}
      {showDiff && pr.diff?.unified_diff && (
        <div className="border-t border-pir px-3 py-2 max-h-[400px] overflow-y-auto">
          <DiffViewer unifiedDiff={pr.diff.unified_diff} />
        </div>
      )}

      {/* Closed reason */}
      {pr.status === "closed" && pr.closed_reason && (
        <div className="border-t border-pir px-3 py-2">
          <div className="text-caption text-pir-text-muted">
            <span className="font-medium">Closed:</span> {pr.closed_reason}
          </div>
        </div>
      )}

      {/* Approval badge */}
      {pr.approved_by && pr.status === "open" && (
        <div className="border-t border-pir px-3 py-2">
          <div className="text-caption text-pir-success">
            Approvata da {pr.approved_by}
          </div>
        </div>
      )}

      {/* Merge error / conflicts */}
      {mergeError && (
        <div className="border-t border-pir px-3 py-2">
          <ErrorAlert message={mergeError} />
          {conflictFiles && (
            <ul className="mt-1 text-caption text-pir-text-muted list-disc pl-4">
              {conflictFiles.map((f) => (
                <li key={f} className="font-mono">{f}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Actions */}
      {!isTerminal && (
        <div className="border-t border-pir px-3 py-2 flex items-center gap-2 flex-wrap">
          <PermissionGate minRole="operator">
            {canClose && (
              <button
                onClick={() => setShowCloseModal(true)}
                className="text-[10px] px-2.5 py-1 rounded border border-pir text-pir-text-muted hover:text-pir-error hover:border-pir-error/40 transition-colors"
              >
                Close PR
              </button>
            )}
            {canReview && !pr.approved_by && (
              <button
                onClick={handleApprove}
                disabled={approving}
                className="text-[10px] px-2.5 py-1 rounded bg-pir-accent text-white hover:bg-pir-accent/80 disabled:opacity-50 transition-colors"
              >
                {approving ? "Approvando..." : "✅ Approva"}
              </button>
            )}
            {canReview && (
              <button
                onClick={() => setShowRequestChanges(true)}
                className="text-[10px] px-2.5 py-1 rounded border border-pir-warning text-pir-warning hover:bg-pir-warning/10 transition-colors"
              >
                ✏️ Request Changes
              </button>
            )}
            {canMerge && (
              <button
                onClick={handleMerge}
                disabled={merging}
                className="text-[10px] px-2.5 py-1 rounded bg-pir-success text-white hover:bg-pir-success/80 disabled:opacity-50 transition-colors ml-auto"
              >
                {merging ? "Merging..." : "Merge"}
              </button>
            )}
          </PermissionGate>
        </div>
      )}

      {/* Close modal */}
      {showCloseModal && (
        <div className="border-t border-pir px-3 py-2 space-y-2">
          <textarea
            value={closeReason}
            onChange={(e) => setCloseReason(e.target.value)}
            placeholder="Reason for closing (feedback for the agent)"
            rows={2}
            className="w-full bg-pir-surface-1 border border-pir rounded px-2 py-1.5 text-xs text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent resize-none"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => {
                setShowCloseModal(false);
                setCloseReason("");
              }}
              className="text-[10px] px-2.5 py-1 rounded border border-pir text-pir-text-muted hover:bg-pir-surface-1 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleClose}
              disabled={closing}
              className="text-[10px] px-2.5 py-1 rounded bg-pir-error text-white hover:bg-pir-error/80 disabled:opacity-50 transition-colors"
            >
              {closing ? "Closing..." : "Close PR"}
            </button>
          </div>
        </div>
      )}

      {/* Request changes modal */}
      {showRequestChanges && (
        <div className="border-t border-pir px-3 py-2 space-y-2">
          <textarea
            value={requestChangesComment}
            onChange={(e) => setRequestChangesComment(e.target.value)}
            placeholder="Commento per l'agente (obbligatorio)"
            rows={2}
            className="w-full bg-pir-surface-1 border border-pir rounded px-2 py-1.5 text-xs text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent resize-none"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => {
                setShowRequestChanges(false);
                setRequestChangesComment("");
              }}
              className="text-[10px] px-2.5 py-1 rounded border border-pir text-pir-text-muted hover:bg-pir-surface-1 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleRequestChanges}
              disabled={requestingChanges || !requestChangesComment.trim()}
              className="text-[10px] px-2.5 py-1 rounded bg-pir-warning text-white hover:bg-pir-warning/80 disabled:opacity-50 transition-colors"
            >
              {requestingChanges ? "Inviando..." : "Request Changes"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
