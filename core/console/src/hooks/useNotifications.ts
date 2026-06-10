"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  listNotifications,
  getUnreadNotificationCount,
  markNotificationRead,
  markNotificationActed,
  markAllNotificationsRead,
} from "@/lib/api";
import type { Notification } from "@/lib/types";

const POLL_INTERVAL = 5_000;

export function useNotifications() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [toasts, setToasts] = useState<Notification[]>([]);
  const controllerRef = useRef<AbortController | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const knownIdsRef = useRef<Set<string>>(new Set());
  const dismissedToastIdsRef = useRef<Set<string>>(new Set());
  const initialLoadRef = useRef(true);
  const refreshRequestIdRef = useRef(0);
  const latestAppliedRequestIdRef = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;

    const refresh = async () => {
      const requestId = ++refreshRequestIdRef.current;
      try {
        const [items, countData] = await Promise.all([
          listNotifications({ limit: 30 }, { signal: controller.signal }),
          getUnreadNotificationCount({ signal: controller.signal }),
        ]);

        if (requestId < latestAppliedRequestIdRef.current) return;
        latestAppliedRequestIdRef.current = requestId;

        // Detect new notifications (not seen before, unread)
        if (!initialLoadRef.current) {
          const newItems = items.filter(
            (n) => !knownIdsRef.current.has(n.id) && !n.read_at
              && !dismissedToastIdsRef.current.has(n.id)
              && n.type !== "task_auto_approved"
          );
          if (newItems.length > 0) {
            setToasts((prev) => {
              const existingIds = new Set(prev.map((t) => t.id));
              const existingTargets = new Set(
                prev.filter((t) => t.target_id).map((t) => `${t.type}:${t.target_id}`)
              );
              // Deduplicate: one toast per target (prefer acted/auto-approved)
              const targetSeen = new Map<string, Notification>();
              for (const n of newItems) {
                if (existingIds.has(n.id)) continue;
                const targetKey = n.target_id ? `task:${n.target_id}` : n.id;
                if (existingTargets.has(`${n.type}:${n.target_id}`)) continue;
                const existing = targetSeen.get(targetKey);
                if (!existing || (n.acted_at && !existing.acted_at)) {
                  targetSeen.set(targetKey, n);
                }
              }
              return [...prev, ...Array.from(targetSeen.values())];
            });
          }
        }

        // Update known IDs
        knownIdsRef.current = new Set(items.map((n) => n.id));
        initialLoadRef.current = false;

        setNotifications(items);
        setUnreadCount(countData.count);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
      } finally {
        setLoading(false);
      }
    };

    const startPolling = () => {
      void refresh();
      if (intervalRef.current) return;
      intervalRef.current = setInterval(refresh, POLL_INTERVAL);
    };

    const stopPolling = () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };

    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        startPolling();
      } else {
        stopPolling();
      }
    };

    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      controller.abort();
      stopPolling();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  // Update document title with unread count
  useEffect(() => {
    document.title =
      unreadCount > 0 ? `(${unreadCount}) Console Marvis` : "Console Marvis";
  }, [unreadCount]);

  const dismissToast = useCallback((id: string, targetId?: string) => {
    dismissedToastIdsRef.current.add(id);
    setToasts((prev) => prev.filter((t) => t.id !== id && (!targetId || t.target_id !== targetId)));
  }, []);

  const markActed = useCallback(async (id: string, _action: string, targetId?: string) => {
    const now = new Date().toISOString();
    dismissedToastIdsRef.current.add(id);
    latestAppliedRequestIdRef.current = refreshRequestIdRef.current + 1;
    setToasts((prev) => prev.filter((t) => t.id !== id && (!targetId || t.target_id !== targetId)));
    setNotifications((prev) =>
      prev.map((n) =>
        n.id === id || (targetId && n.type === "task_pending" && n.target_id === targetId)
          ? { ...n, acted_at: now, read_at: n.read_at || now }
          : n
      )
    );
    setUnreadCount((prev) => Math.max(0, prev - 1));
    try {
      await markNotificationActed(id);
    } catch {
      // Next poll will correct
    }
  }, []);

  const markRead = useCallback(async (id: string) => {
    dismissedToastIdsRef.current.add(id);
    latestAppliedRequestIdRef.current = refreshRequestIdRef.current + 1;
    setToasts((prev) => prev.filter((t) => t.id !== id));
    setNotifications((prev) =>
      prev.map((n) =>
        n.id === id ? { ...n, read_at: new Date().toISOString() } : n
      )
    );
    setUnreadCount((prev) => Math.max(0, prev - 1));
    try {
      await markNotificationRead(id);
    } catch {
      // Revert on failure — next poll will correct
    }
  }, []);

  const markAllRead = useCallback(async () => {
    const prevNotifications = notifications;
    const prevCount = unreadCount;
    dismissedToastIdsRef.current = new Set(notifications.map((n) => n.id));
    latestAppliedRequestIdRef.current = refreshRequestIdRef.current + 1;
    setToasts([]);
    setNotifications((prev) =>
      prev.map((n) =>
        n.read_at ? n : { ...n, read_at: new Date().toISOString() }
      )
    );
    setUnreadCount(0);
    try {
      await markAllNotificationsRead();
    } catch {
      setNotifications(prevNotifications);
      setUnreadCount(prevCount);
    }
  }, [notifications, unreadCount]);

  return {
    notifications,
    unreadCount,
    loading,
    toasts,
    markRead,
    markAllRead,
    markActed,
    dismissToast,
  };
}
