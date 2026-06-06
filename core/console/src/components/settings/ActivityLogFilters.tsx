// v1.0.0 - 2026-03-13 - Filter bar for activity log
"use client";

import type { AuditEventType } from "@/lib/types";

interface ActivityLogFiltersProps {
  eventType: string;
  period: number;
  onEventTypeChange: (v: string) => void;
  onPeriodChange: (v: number) => void;
}

const EVENT_TYPES: { value: string; label: string }[] = [
  { value: "", label: "All events" },
  { value: "login", label: "Login" },
  { value: "logout", label: "Logout" },
  { value: "role_changed", label: "Role changed" },
  { value: "team_joined", label: "Team joined" },
  { value: "team_left", label: "Team left" },
  { value: "user_invited", label: "User invited" },
  { value: "sso_configured", label: "SSO configured" },
  { value: "task_completed", label: "Task completed" },
  { value: "pr_merged", label: "PR merged" },
];

const PERIODS = [
  { value: 7, label: "Last 7 days" },
  { value: 30, label: "Last 30 days" },
  { value: 90, label: "Last 90 days" },
];

export default function ActivityLogFilters({
  eventType,
  period,
  onEventTypeChange,
  onPeriodChange,
}: ActivityLogFiltersProps) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <select
        value={eventType}
        onChange={(e) => onEventTypeChange(e.target.value)}
        className="bg-pir-surface-0 border border-pir rounded px-2.5 py-1 text-xs text-pir-text-secondary focus:outline-none focus:border-pir-accent"
      >
        {EVENT_TYPES.map((et) => (
          <option key={et.value} value={et.value}>{et.label}</option>
        ))}
      </select>
      <select
        value={period}
        onChange={(e) => onPeriodChange(Number(e.target.value))}
        className="bg-pir-surface-0 border border-pir rounded px-2.5 py-1 text-xs text-pir-text-secondary focus:outline-none focus:border-pir-accent"
      >
        {PERIODS.map((p) => (
          <option key={p.value} value={p.value}>{p.label}</option>
        ))}
      </select>
    </div>
  );
}
