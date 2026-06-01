"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { parseZombieReportBody, type Notification } from "@/lib/types";
import { bulkRejectTasks, updateTask } from "@/lib/api";

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr + (dateStr.endsWith("Z") ? "" : "Z")).getTime();
  const seconds = Math.floor((now - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

interface NotificationItemProps {
  notification: Notification;
  onMarkRead: (id: string) => void;
  onMarkActed: (id: string, action: string, targetId?: string) => void;
  onClose: () => void;
}

export function NotificationItem({
  notification,
  onMarkRead,
  onMarkActed,
  onClose,
}: NotificationItemProps) {
  const router = useRouter();
  const [inflight, setInflight] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isUnread = !notification.read_at;
  const isActed = !!notification.acted_at;
  const isDeploy = notification.type === "deploy_failed";
  const isZombieReport = notification.type === "task_zombie_report";
  const zombieBody = isZombieReport ? parseZombieReportBody(notification.body) : null;

  const handleAction = async (action: "approved" | "rejected") => {
    if (inflight) return;
    setInflight(action);
    setError(null);
    try {
      await updateTask(notification.target_id, { status: action });
      onMarkActed(notification.id, action, notification.target_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed");
      setTimeout(() => setError(null), 3000);
    } finally {
      setInflight(null);
    }
  };

  const handleBulkReject = async () => {
    if (inflight || !zombieBody) return;
    const confirmed = window.confirm(
      `Rifiutare ${zombieBody.count} task zombie di ${zombieBody.project}? L'azione non e reversibile.`
    );
    if (!confirmed) return;
    setInflight("bulk_reject");
    setError(null);
    try {
      const result = await bulkRejectTasks(zombieBody.task_ids, "aging_zombie");
      // Mark as acted — the notification itself does not have a target_id,
      // so pass undefined to avoid matching unrelated task_pending notifications.
      onMarkActed(notification.id, "bulk_rejected", undefined);
      const failedCount = result.failed.length;
      if (failedCount > 0) {
        setError(`${result.rejected.length} rejected, ${failedCount} failed`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Bulk reject failed");
      setTimeout(() => setError(null), 5000);
    } finally {
      setInflight(null);
    }
  };

  const handleView = () => {
    onMarkRead(notification.id);
    onClose();
    router.push(`/triage/?task=${encodeURIComponent(notification.target_id)}`);
  };

  return (
    <div
      className={`px-3 py-2.5 border-b border-pir last:border-b-0 ${
        isDeploy
          ? "bg-red-500/10 border-l-2 border-l-red-500"
          : isUnread ? "bg-pir-surface-2/50" : ""
      }`}
    >
      <div className="flex items-start gap-2">
        {/* Read/unread dot */}
        <span
          className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${
            isDeploy && isUnread ? "bg-red-500" :
            isUnread ? "bg-pir-accent" : "bg-pir-text-muted/30"
          }`}
        />

        <div className="flex-1 min-w-0">
          {/* Title + timestamp */}
          <div className="flex items-baseline justify-between gap-2">
            <span className={`text-caption font-medium truncate ${
              isDeploy ? "text-red-700 dark:text-red-400" : "text-pir-text-primary"
            }`}>
              {notification.title}
            </span>
            <span className="text-[11px] text-pir-text-muted shrink-0">
              {timeAgo(notification.created_at)}
            </span>
          </div>

          {/* Body */}
          {notification.body && (
            <p className="text-caption text-pir-text-secondary mt-0.5 truncate">
              {notification.body}
            </p>
          )}

          {/* Project */}
          {notification.project && (
            <p className="text-[11px] text-pir-text-muted mt-0.5">
              {notification.project}
            </p>
          )}

          {/* Error */}
          {error && (
            <p className="text-[11px] text-pir-error mt-1">{error}</p>
          )}

          {/* Auto-approved badge (purple) */}
          {isActed && notification.type === "task_auto_approved" && (
            <span className="inline-flex items-center gap-1 mt-1 px-2 py-0.5 text-[11px] font-medium rounded bg-purple-500/15 text-purple-700 dark:text-purple-400 border border-purple-500/25">
              Auto-approved
            </span>
          )}

          {/* Generic acted badge */}
          {isActed && notification.type !== "task_auto_approved" && (
            <p className="text-[11px] mt-1 font-medium text-pir-text-muted">
              Acted
            </p>
          )}

          {/* Actions — only show if not acted */}
          {!isActed && (
            <div className="flex items-center gap-1.5 mt-1.5">
              {notification.type === "task_pending" && (
                <>
                  <button
                    onClick={() => handleAction("approved")}
                    disabled={!!inflight}
                    className="px-2 py-0.5 text-[11px] font-medium rounded bg-emerald-600/20 text-emerald-700 dark:text-emerald-400 hover:bg-emerald-600/30 disabled:opacity-50 transition-colors"
                  >
                    {inflight === "approved" ? "..." : "Approve"}
                  </button>
                  <button
                    onClick={() => handleAction("rejected")}
                    disabled={!!inflight}
                    className="px-2 py-0.5 text-[11px] font-medium rounded bg-pir-error/20 text-pir-error hover:bg-pir-error/30 disabled:opacity-50 transition-colors"
                  >
                    {inflight === "rejected" ? "..." : "Reject"}
                  </button>
                </>
              )}
              {isZombieReport && zombieBody && (
                <button
                  type="button"
                  onClick={handleBulkReject}
                  disabled={!!inflight}
                  className="px-2 py-0.5 text-[11px] font-medium rounded bg-pir-error/20 text-pir-error hover:bg-pir-error/30 disabled:opacity-50 transition-colors"
                >
                  {inflight === "bulk_reject"
                    ? "..."
                    : `Rifiuta ${zombieBody.count} zombie`}
                </button>
              )}
              {notification.type !== "deploy_failed"
                && notification.type !== "deploy_success"
                && !isZombieReport && (
                <button
                  onClick={handleView}
                  className="px-2 py-0.5 text-[11px] text-pir-text-muted hover:text-pir-text-secondary transition-colors"
                >
                  View
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
