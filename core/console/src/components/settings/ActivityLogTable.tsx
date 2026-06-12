// v1.0.0 - 2026-03-13 - Activity log table with event type icons
"use client";

import type { AuditLogEntry, AuditEventType } from "@/lib/types";

interface ActivityLogTableProps {
  entries: AuditLogEntry[];
  loading: boolean;
}

const EVENT_ICONS: Record<AuditEventType, { icon: React.ReactNode; color: string }> = {
  login: {
    icon: <path d="M9 3l6 6-6 6M15 9H3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />,
    color: "text-emerald-500",
  },
  logout: {
    icon: <path d="M9 3l-6 6 6 6M3 9h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />,
    color: "text-gray-400",
  },
  role_changed: {
    icon: <path d="M9 2l2.5 5H15l-3.5 3 1.5 5L9 12.5 5 15l1.5-5L3 7h3.5L9 2z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />,
    color: "text-amber-500",
  },
  team_joined: {
    icon: <><circle cx="7" cy="6" r="2.5" stroke="currentColor" strokeWidth="1.2" /><path d="M2 15c0-2.8 2.2-5 5-5s5 2.2 5 5M14 8v4M16 10h-4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" /></>,
    color: "text-blue-500",
  },
  team_left: {
    icon: <><circle cx="7" cy="6" r="2.5" stroke="currentColor" strokeWidth="1.2" /><path d="M2 15c0-2.8 2.2-5 5-5s5 2.2 5 5M13 10h4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" /></>,
    color: "text-orange-500",
  },
  user_invited: {
    icon: <><circle cx="9" cy="6" r="3" stroke="currentColor" strokeWidth="1.2" /><path d="M3 16c0-3.3 2.7-6 6-6s6 2.7 6 6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" /></>,
    color: "text-pir-accent",
  },
  sso_configured: {
    icon: <path d="M12 2a5 5 0 00-5 5v3H5a1 1 0 00-1 1v5a1 1 0 001 1h8a1 1 0 001-1v-5a1 1 0 00-1-1h-2V7a3 3 0 016 0" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />,
    color: "text-violet-500",
  },
  workspace_created: {
    icon: <path d="M4 4h10v10H4zM7 4V2M11 4V2M4 8h10" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />,
    color: "text-teal-500",
  },
  task_completed: {
    icon: <path d="M4 9l4 4L16 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />,
    color: "text-emerald-500",
  },
  pr_merged: {
    icon: <><circle cx="6" cy="4" r="2" stroke="currentColor" strokeWidth="1.2" /><circle cx="6" cy="14" r="2" stroke="currentColor" strokeWidth="1.2" /><circle cx="14" cy="10" r="2" stroke="currentColor" strokeWidth="1.2" /><path d="M6 6v6M12 10H8c0-2.2 1.8-4 4-4" stroke="currentColor" strokeWidth="1.2" /></>,
    color: "text-purple-500",
  },
};

export default function ActivityLogTable({ entries, loading }: ActivityLogTableProps) {
  if (loading && entries.length === 0) {
    return (
      <div className="space-y-2">
        {[1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="animate-pulse h-10 bg-pir-surface-0 rounded" />
        ))}
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="text-center py-12 text-sm text-pir-text-muted">
        No activity yet
      </div>
    );
  }

  return (
    <div className="space-y-0.5">
      {entries.map((entry) => {
        const eventConfig = EVENT_ICONS[entry.event_type] ?? {
          icon: <circle cx="9" cy="9" r="4" stroke="currentColor" strokeWidth="1.2" />,
          color: "text-gray-400",
        };
        return (
          <div
            key={entry.id}
            className="flex items-center gap-3 px-3 py-2.5 rounded hover:bg-pir-surface-1/50 transition-colors"
          >
            <span className="text-[10px] text-pir-text-muted w-12 shrink-0">
              {formatTime(entry.timestamp)}
            </span>
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none" className={`shrink-0 ${eventConfig.color}`}>
              {eventConfig.icon}
            </svg>
            <span className="text-xs font-medium text-pir-text-secondary w-20 shrink-0 truncate">
              {entry.user_name}
            </span>
            <span className="text-xs text-pir-text-primary flex-1 min-w-0 truncate">
              {entry.description}
            </span>
            <span className="text-[10px] text-pir-text-muted shrink-0 hidden sm:block">
              {formatDate(entry.timestamp)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}
