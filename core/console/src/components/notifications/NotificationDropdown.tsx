"use client";

import type { Notification } from "@/lib/types";
import { usePushSubscription } from "@/hooks/usePushSubscription";
import { NotificationItem } from "./NotificationItem";

interface NotificationDropdownProps {
  notifications: Notification[];
  unreadCount: number;
  onMarkRead: (id: string) => void;
  onMarkActed: (id: string, action: string, targetId?: string) => void;
  onMarkAllRead: () => void;
  onClose: () => void;
}

export function NotificationDropdown({
  notifications,
  unreadCount,
  onMarkRead,
  onMarkActed,
  onMarkAllRead,
  onClose,
}: NotificationDropdownProps) {
  const { state: pushState, subscribe, unsubscribe } = usePushSubscription();

  return (
    <div className="absolute right-0 top-full mt-1 w-[340px] max-h-[400px] bg-pir-surface-1 border border-pir rounded-lg shadow-lg z-50 flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-pir shrink-0">
        <span className="text-caption font-medium text-pir-text-primary">
          Notifiche{unreadCount > 0 ? ` (${unreadCount})` : ""}
        </span>
        {unreadCount > 0 && (
          <button
            onClick={onMarkAllRead}
            className="text-[11px] text-pir-accent hover:text-pir-accent/80 transition-colors"
          >
            Mark all read
          </button>
        )}
      </div>

      {/* Push notification soft-ask banner */}
      {pushState === "prompt" && (
        <div className="px-3 py-2 border-b border-pir bg-pir-surface-2/50 shrink-0">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] text-pir-text-secondary">
              Ricevi notifiche push?
            </span>
            <button
              onClick={subscribe}
              className="text-[11px] px-2 py-0.5 rounded bg-pir-accent text-white hover:bg-pir-accent/80 transition-colors"
            >
              Attiva
            </button>
          </div>
        </div>
      )}
      {pushState === "subscribed" && (
        <div className="px-3 py-1.5 border-b border-pir bg-pir-surface-2/50 shrink-0">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] text-pir-text-muted">
              Push attive
            </span>
            <button
              onClick={unsubscribe}
              className="text-[11px] text-pir-text-muted hover:text-pir-error transition-colors"
            >
              Disattiva
            </button>
          </div>
        </div>
      )}
      {pushState === "denied" && (
        <div className="px-3 py-1.5 border-b border-pir shrink-0">
          <span className="text-[11px] text-pir-text-muted">
            Push bloccate dal browser. Abilita dalle impostazioni del sito.
          </span>
        </div>
      )}

      {/* Notification list */}
      <div className="overflow-y-auto flex-1">
        {notifications.length === 0 ? (
          <div className="px-3 py-8 text-center text-caption text-pir-text-muted">
            Nessuna notifica
          </div>
        ) : (
          notifications.map((n) => (
            <NotificationItem
              key={n.id}
              notification={n}
              onMarkRead={onMarkRead}
              onMarkActed={onMarkActed}
              onClose={onClose}
            />
          ))
        )}
      </div>
    </div>
  );
}
