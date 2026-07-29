// v1.0.0 - 2026-03-13 - Filter bar for activity log
"use client";

interface ActivityLogFiltersProps {
  action: string;
  onActionChange: (v: string) => void;
}

const ACTION_FILTERS: { value: string; label: string }[] = [
  { value: "", label: "All actions" },
  { value: "task.", label: "Tasks" },
  { value: "pr.", label: "Pull requests" },
  { value: "todo.", label: "Todos" },
  { value: "tool_call", label: "Tool calls" },
  { value: "delegation.", label: "Delegations" },
  { value: "check_learnings", label: "Learning checks" },
  { value: "docs_triage_bot", label: "Docs triage" },
];

export default function ActivityLogFilters({
  action,
  onActionChange,
}: ActivityLogFiltersProps) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <select
        value={action}
        onChange={(e) => onActionChange(e.target.value)}
        className="bg-pir-surface-0 border border-pir rounded px-2.5 py-1 text-xs text-pir-text-secondary focus:outline-none focus:border-pir-accent"
      >
        {ACTION_FILTERS.map((item) => (
          <option key={item.value} value={item.value}>{item.label}</option>
        ))}
      </select>
    </div>
  );
}
