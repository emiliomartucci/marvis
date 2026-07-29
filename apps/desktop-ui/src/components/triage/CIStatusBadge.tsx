// v1.0.0 - 2026-03-13 - Compact CI merge gate badge with color coding
"use client";

import type { CIChecksSummary } from "@/lib/types";

interface CIStatusBadgeProps {
  summary: CIChecksSummary;
  onClick?: () => void;
}

export default function CIStatusBadge({ summary, onClick }: CIStatusBadgeProps) {
  const { total, passed, failed, pending, merge_blocked } = summary;

  if (total === 0) {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400">
        <NoCIIcon />
        No CI
      </span>
    );
  }

  const allPassed = passed === total && failed === 0 && pending === 0;
  const hasPending = pending > 0;

  let colorClasses: string;
  let label: string;
  let icon: React.ReactNode;

  if (allPassed) {
    colorClasses = "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400";
    label = "CI Passed";
    icon = <CheckIcon />;
  } else if (failed > 0) {
    colorClasses = "bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-400";
    label = `CI Failing`;
    icon = <XIcon />;
  } else if (hasPending) {
    colorClasses = "bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400";
    label = "Running";
    icon = <SpinnerIcon />;
  } else {
    colorClasses = "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400";
    label = "CI";
    icon = null;
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full transition-opacity hover:opacity-80 ${colorClasses}`}
    >
      {icon}
      {label}
      <span className="opacity-60">{passed}/{total}</span>
      {merge_blocked && (
        <span className="ml-0.5 text-red-600 dark:text-red-400 font-semibold">blocked</span>
      )}
    </button>
  );
}

function CheckIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 12 12" fill="none" className="shrink-0">
      <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 12 12" fill="none" className="shrink-0">
      <path d="M3 3L9 9M9 3L3 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function NoCIIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 12 12" fill="none" className="shrink-0">
      <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.2" />
      <path d="M4 6H8" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 12 12" fill="none" className="shrink-0 animate-spin">
      <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.2" opacity="0.3" />
      <path d="M6 2a4 4 0 012.83 1.17" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}
