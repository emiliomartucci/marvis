"use client";

import { useState } from "react";
import { useNotifications } from "@/hooks/useNotifications";
import { NotificationDropdown } from "./NotificationDropdown";
import { NotificationToastStack } from "./NotificationToast";

export function NotificationBell() {
  const {
    notifications,
    unreadCount,
    markRead,
    markAllRead,
    markActed,
    toasts,
    dismissToast,
  } = useNotifications();
  const [open, setOpen] = useState(false);

  const hasCritical = notifications.some(
    (n) => !n.read_at && n.type === "deploy_failed"
  );

  return (
    <>
      {/* Invisible backdrop to catch clicks outside dropdown */}
      {open && (
        <div
          className="fixed inset-0 z-40"
          onClick={() => setOpen(false)}
        />
      )}

      <div className="relative">
        <button
          onClick={() => setOpen((prev) => !prev)}
          className={`p-1.5 rounded transition-colors ${
            open
              ? "text-pir-accent"
              : "text-pir-text-muted hover:text-pir-text-secondary"
          }`}
          aria-label="Notifications"
        >
          {/* Bell icon */}
          <svg
            xmlns="http://www.w3.org/2000/svg"
            className="w-4 h-4"
            viewBox="0 0 20 20"
            fill="currentColor"
          >
            <path d="M10 2a6 6 0 00-6 6v3.586l-.707.707A1 1 0 004 14h12a1 1 0 00.707-1.707L16 11.586V8a6 6 0 00-6-6zM10 18a3 3 0 01-3-3h6a3 3 0 01-3 3z" />
          </svg>

          {/* Badge */}
          {unreadCount > 0 && (
            <span className={`absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 flex items-center justify-center rounded-full bg-pir-error text-white text-[9px] font-bold leading-none ${
              hasCritical ? "animate-pulse ring-2 ring-red-500/50" : ""
            }`}>
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </button>

        {open && (
          <NotificationDropdown
            notifications={notifications}
            unreadCount={unreadCount}
            onMarkRead={markRead}
            onMarkActed={markActed}
            onMarkAllRead={markAllRead}
            onClose={() => setOpen(false)}
          />
        )}
      </div>

      {/* Toast stack — renders outside the bell container, fixed position */}
      <NotificationToastStack
        toasts={toasts}
        onDismiss={dismissToast}
        onMarkRead={markRead}
        onMarkActed={markActed}
      />
    </>
  );
}
