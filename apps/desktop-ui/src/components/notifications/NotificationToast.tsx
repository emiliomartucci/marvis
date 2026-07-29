"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { Notification } from "@/lib/types";
import { updateTask } from "@/lib/api";

const AUTO_DISMISS_MS = 8_000;

interface NotificationToastProps {
  notification: Notification;
  onDismiss: (id: string) => void;
  onMarkRead: (id: string) => void;
  onMarkActed: (id: string, action: string, targetId?: string) => void;
}

function SingleToast({
  notification,
  onDismiss,
  onMarkRead,
  onMarkActed,
}: NotificationToastProps) {
  const router = useRouter();
  const [inflight, setInflight] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [exiting, setExiting] = useState(false);
  const isDeploy = notification.type === "deploy_failed";

  // Auto-dismiss after AUTO_DISMISS_MS — paused while an action is in flight
  // (so the user never sees the toast vanish before their click completes),
  // and paused while we are showing an inline error so they can read it.
  useEffect(() => {
    if (inflight || actionError) return;
    const timer = setTimeout(() => {
      setExiting(true);
      setTimeout(() => onDismiss(notification.id), 300);
    }, AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [notification.id, onDismiss, inflight, actionError]);

  const handleAction = async (action: "approved" | "rejected") => {
    if (inflight) return;
    // Guard: target_id must be present to PATCH /api/v1/tasks/<id>.
    // Without it the request would hit /api/v1/tasks/ (no id) and 404 silently.
    if (!notification.target_id) {
      console.error(
        "NotificationToast: missing target_id on notification",
        notification.id,
        notification.type,
      );
      setActionError("Missing task id");
      return;
    }
    setInflight(action);
    setActionError(null);
    try {
      await updateTask(notification.target_id, { status: action });
      setActionResult(action === "approved" ? "Approved" : "Rejected");
      onMarkActed(notification.id, action, notification.target_id);
      setTimeout(() => onDismiss(notification.id), 1500);
    } catch (err) {
      console.error(
        "NotificationToast: failed to",
        action,
        "task",
        notification.target_id,
        err,
      );
      setInflight(null);
      const message = err instanceof Error ? err.message : "Action failed";
      setActionError(message);
    }
  };

  const handleView = () => {
    onMarkRead(notification.id);
    onDismiss(notification.id);
    router.push(`/triage/?task=${encodeURIComponent(notification.target_id)}`);
  };

  const handleDismiss = () => {
    setExiting(true);
    setTimeout(() => onDismiss(notification.id), 300);
  };

  return (
    <div
      className={`w-[360px] bg-pir-surface-1 border rounded-lg shadow-xl transition-all duration-300 motion-reduce:translate-x-0 motion-reduce:transition-none ${
        isDeploy ? "border-red-500/50 border-l-2 border-l-red-500 bg-red-500/5" : "border-pir"
      } ${
        exiting
          ? "opacity-0 translate-x-4"
          : "opacity-100 translate-x-0 animate-slide-in-right"
      }`}
    >
      <div className="px-3.5 py-3">
        {/* Header: title + dismiss */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1 min-w-0">
            <span className={`text-caption font-medium ${
              isDeploy ? "text-red-700 dark:text-red-400" : "text-pir-text-primary"
            }`}>
              {notification.title}
            </span>
            {notification.body && (
              <p className="text-caption text-pir-text-secondary mt-0.5 truncate">
                {notification.body}
              </p>
            )}
            {notification.project && (
              <span className="text-[11px] text-pir-text-muted">
                {notification.project}
              </span>
            )}
          </div>
          <button
            onClick={handleDismiss}
            className="shrink-0 p-0.5 text-pir-text-muted hover:text-pir-text-secondary transition-colors"
            aria-label="Dismiss"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
                clipRule="evenodd"
              />
            </svg>
          </button>
        </div>

        {/* Action result */}
        {actionResult && (
          <p
            className={`text-[11px] mt-1.5 font-medium ${
              actionResult === "Approved"
                ? "text-emerald-700 dark:text-emerald-400"
                : "text-pir-error"
            }`}
          >
            {actionResult}
          </p>
        )}

        {/* Action error — visible so user knows the click didn't complete */}
        {actionError && !actionResult && (
          <p
            className="text-[11px] mt-1.5 font-medium text-pir-error"
            role="alert"
          >
            Error: {actionError}
          </p>
        )}

        {/* Auto-approved badge */}
        {!actionResult && notification.acted_at && notification.type === "task_auto_approved" && (
          <span className="inline-flex items-center gap-1 mt-2 px-2 py-0.5 text-[11px] font-medium rounded bg-purple-500/15 text-purple-700 dark:text-purple-400 border border-purple-500/25">
            Auto-approved
          </span>
        )}

        {/* Action buttons — only if not acted */}
        {!actionResult && !notification.acted_at && (
          <div className="flex items-center gap-1.5 mt-2">
            {notification.type === "task_pending" && (
              <>
                <button
                  onClick={() => handleAction("approved")}
                  disabled={!!inflight}
                  className="px-2.5 py-1 text-[11px] font-medium rounded bg-emerald-600/20 text-emerald-700 dark:text-emerald-400 hover:bg-emerald-600/30 disabled:opacity-50 transition-colors"
                >
                  {inflight === "approved" ? "..." : "Approve"}
                </button>
                <button
                  onClick={() => handleAction("rejected")}
                  disabled={!!inflight}
                  className="px-2.5 py-1 text-[11px] font-medium rounded bg-pir-error/20 text-pir-error hover:bg-pir-error/30 disabled:opacity-50 transition-colors"
                >
                  {inflight === "rejected" ? "..." : "Reject"}
                </button>
              </>
            )}
            {notification.type !== "deploy_failed" && notification.type !== "deploy_success" && (
              <button
                onClick={handleView}
                className="px-2.5 py-1 text-[11px] text-pir-text-muted hover:text-pir-text-secondary transition-colors"
              >
                View
              </button>
            )}
          </div>
        )}
      </div>

      {/* Auto-dismiss progress bar */}
      <div className="h-0.5 bg-pir-surface-2 rounded-b-lg overflow-hidden">
        <div
          className={`h-full animate-shrink-width motion-reduce:animate-none ${isDeploy ? "bg-red-500/40" : "bg-pir-accent/40"}`}
          style={{ animationDuration: `${AUTO_DISMISS_MS}ms` }}
        />
      </div>
    </div>
  );
}

interface NotificationToastStackProps {
  toasts: Notification[];
  onDismiss: (id: string) => void;
  onMarkRead: (id: string) => void;
  onMarkActed: (id: string, action: string, targetId?: string) => void;
}

export function NotificationToastStack({
  toasts,
  onDismiss,
  onMarkRead,
  onMarkActed,
}: NotificationToastStackProps) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-12 right-4 z-50 flex flex-col gap-2">
      {toasts.map((toast) => (
        <SingleToast
          key={toast.id}
          notification={toast}
          onDismiss={onDismiss}
          onMarkRead={onMarkRead}
          onMarkActed={onMarkActed}
        />
      ))}
    </div>
  );
}
