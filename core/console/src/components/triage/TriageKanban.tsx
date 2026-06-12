"use client";

// v1.1.0 - 2026-04-22 - theme-v2 polish: dot headers, delegation border-left,
// card structural layout, review card inline (gated behind .theme-v2).

import { useCallback, useEffect, useMemo, useRef, useState, memo } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  closestCorners,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
  useDroppable,
} from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { TaskResponse, PullRequest, PrDiff, MergeConflictResponse } from "@/lib/types";
import { updateTask, getPullRequest, mergePullRequest, closePullRequest, revertPullRequest, getMergeConflicts } from "@/lib/api";
import { ErrorAlert } from "@/components/ui/ErrorAlert";
import { useDesignV2 } from "@/lib/useDesignV2";

// Active columns (droppable, sortable)
const COLUMNS = ["pending", "approved", "in_progress", "review"] as const;
type Column = (typeof COLUMNS)[number];

// All statuses including terminal ones
type AnyStatus = Column | "completed" | "failed" | "rejected";

const COLUMN_LABELS: Record<AnyStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  in_progress: "Working",
  review: "Review",
  completed: "Completed",
  failed: "Failed",
  rejected: "Rejected",
};

const COLUMN_COLORS: Record<AnyStatus, string> = {
  pending: "bg-pir-warning",
  approved: "bg-pir-accent",
  in_progress: "bg-pir-success",
  review: "bg-purple-500",
  completed: "bg-pir-success",
  failed: "bg-pir-error",
  rejected: "bg-pir-text-muted",
};

// Next valid transition for quick-advance button
// review has no next — requires explicit merge/close via PR buttons
const NEXT_STATUS: Record<string, string> = {
  pending: "approved",
  approved: "in_progress",
};

const DELEGATION_COLORS: Record<string, string> = {
  agent: "border-l-emerald-500",
  hybrid: "border-l-blue-500",
  human: "border-l-amber-500",
};

const DELEGATION_BADGE: Record<string, string> = {
  agent: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  hybrid: "bg-blue-500/15 text-blue-700 dark:text-blue-400",
  human: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
};

const PRIORITY_BADGES: Record<string, string> = {
  high: "bg-pir-error/10 text-pir-error",
  medium: "bg-pir-warning/10 text-pir-warning",
  low: "bg-pir-text-muted/10 text-pir-text-muted",
};

const KIND_BADGES: Record<string, string> = {
  idea: "bg-fuchsia-500/15 text-fuchsia-700 dark:text-fuchsia-400",
};

const PAGE_SIZE = 30;

// --- Description parser ---
// Parses: "Devo {azione} perché {problema}. Attenzione a {dipendenze}."
interface ParsedDesc {
  action: string | null;
  problem: string | null;
  attention: string | null;
}

function parseDescription(desc: string | null | undefined): ParsedDesc {
  if (!desc) return { action: null, problem: null, attention: null };
  const actionMatch = desc.match(/[Dd]evo\s+(.+?)\s+perch[eé]/i);
  const problemMatch = desc.match(/perch[eé]\s+([\s\S]+?)(?:\.\s*[Aa]ttenzione|\.\s*-\/|\s*$)/i);
  const attentionMatch = desc.match(/[Aa]ttenzione\s+a\s+([\s\S]+?)(?:\s*-\/|\s*$)/i);
  return {
    action: actionMatch?.[1]?.trim().replace(/\s+/g, " ") || null,
    problem: problemMatch?.[1]?.trim().replace(/\s+/g, " ") || null,
    attention: attentionMatch?.[1]?.trim().replace(/\s+/g, " ") || null,
  };
}

// --- Droppable Column ---

function DroppableColumn({
  id,
  count,
  children,
}: {
  id: Column;
  count: number;
  children: React.ReactNode;
}) {
  const isReviewCol = id === "review";
  const { setNodeRef, isOver } = useDroppable({ id, disabled: isReviewCol });
  return (
    <div
      ref={setNodeRef}
      className={`flex-1 min-w-[220px] bg-pir-surface-0 rounded-lg p-2 transition-colors ${
        isOver && !isReviewCol ? "ring-1 ring-pir-accent" : ""
      } ${isReviewCol ? "border border-dashed border-purple-500/30" : ""}`}
    >
      <h3 className="text-caption font-medium text-pir-text-muted mb-2 px-1 flex items-center gap-1.5">
        <span className={`w-2 h-2 rounded-full ${COLUMN_COLORS[id]}`} />
        {COLUMN_LABELS[id]}
        <span className="text-pir-text-muted ml-auto">{count}</span>
      </h3>
      <div className="space-y-1 min-h-[100px]">{children}</div>
    </div>
  );
}

// --- Static Column (completed/rejected — no drag) ---

function StaticColumn({
  id,
  count,
  collapsed,
  onToggle,
  children,
}: {
  id: AnyStatus;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className={`bg-pir-surface-0 rounded-lg p-2 transition-all ${collapsed ? "min-w-[48px] w-[48px]" : "flex-1 min-w-[220px]"}`}>
      <h3
        className="text-caption font-medium text-pir-text-muted mb-2 px-1 flex items-center gap-1.5 cursor-pointer select-none"
        onClick={onToggle}
      >
        <span className={`w-2 h-2 rounded-full shrink-0 ${COLUMN_COLORS[id]}`} />
        {!collapsed && (
          <>
            {COLUMN_LABELS[id]}
            <span className="text-pir-text-muted ml-auto">{count}</span>
          </>
        )}
        {collapsed && (
          <span className="text-pir-text-muted text-[9px] writing-vertical" style={{ writingMode: "vertical-rl", textOrientation: "mixed" }}>
            {COLUMN_LABELS[id]} ({count})
          </span>
        )}
      </h3>
      {!collapsed && <div className="space-y-1 min-h-[60px]">{children}</div>}
    </div>
  );
}

// --- Sortable Triage Card ---

interface MergeOrderInfo {
  position: number;
  total: number;
  canMerge: boolean;
  blockedBy: string | null;
  blockedByTitle: string | null;
}

const TriageCard = memo(function TriageCard({
  task,
  isInflight,
  onClick,
  onQuickAction,
  onMerge,
  onClose,
  showActions,
  prDiff,
  mergeOrder,
  prTitle,
  prBranch,
}: {
  task: TaskResponse;
  isInflight: boolean;
  onClick: (task: TaskResponse) => void;
  onQuickAction: (taskId: string, newStatus: string) => void;
  onMerge?: (taskId: string) => void;
  onClose?: (taskId: string) => void;
  showActions: boolean;
  prDiff?: PrDiff | null;
  mergeOrder?: MergeOrderInfo | null;
  prTitle?: string | null;
  prBranch?: string | null;
}) {
  const isReview = task.status === "review";
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: task.id,
    disabled: isInflight || isReview,
  });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : isInflight ? 0.6 : 1,
  };

  const borderColor = task.delegation ? DELEGATION_COLORS[task.delegation] || "" : "border-l-pir-text-muted/30";
  const nextStatus = NEXT_STATUS[task.status];
  const parsedDesc = task.description ? parseDescription(task.description) : null;
  const hasDesc = parsedDesc && (parsedDesc.action || parsedDesc.problem || parsedDesc.attention);

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      onClick={() => !isDragging && onClick(task)}
      className={`bg-pir-surface-1 border border-pir rounded px-3 py-2 cursor-grab active:cursor-grabbing border-l-2 ${borderColor} hover:border-pir-accent transition-colors group relative`}
    >
      {/* Title */}
      <div className="text-label text-pir-text-primary line-clamp-2 pr-12 leading-snug">{task.title}</div>
      <div className="text-[10px] text-pir-text-muted/60 truncate mt-0.5 flex items-center gap-1.5">
        <span
          className="text-[9px] tabular-nums cursor-pointer hover:text-pir-text-muted"
          title="Click to copy full task ID"
          onClick={(e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(task.id);
          }}
        >
          #{task.id.slice(0, 8)}
        </span>
        {task.created_by && <span>by {task.created_by}</span>}
      </div>

      {/* Description color coding — problem + attention only (action duplicates title) */}
      {(parsedDesc?.problem || parsedDesc?.attention) && (
        <div className="mt-1.5 pt-1 border-t border-pir/40 space-y-0.5">
          {parsedDesc.problem && (
            <div className="text-[10px] text-rose-600 dark:text-rose-400/80 line-clamp-1 leading-tight">
              <span className="text-rose-600/60 mr-1">!</span>{parsedDesc.problem}
            </div>
          )}
          {parsedDesc.attention && (
            <div className="text-[10px] text-amber-700 dark:text-amber-400/80 line-clamp-1 leading-tight">
              <span className="text-amber-600/60 mr-1">◆</span>{parsedDesc.attention}
            </div>
          )}
        </div>
      )}

      {/* Score + badges */}
      <div className="flex items-center gap-1 mt-1.5 flex-wrap">
        {task.ice_score != null && (
          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-pir-accent/15 text-pir-accent tabular-nums">
            {task.ice_score}
          </span>
        )}
        {task.delegation && (
          <span className={`text-[9px] px-1 rounded ${DELEGATION_BADGE[task.delegation] || ""}`}>
            {task.delegation}
          </span>
        )}
        <span className="text-[9px] px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-400 font-medium truncate max-w-[80px]">
          {task.project}
        </span>
        <span className={`text-[9px] px-1 rounded ${PRIORITY_BADGES[task.priority] || ""}`}>
          {task.priority === "high" ? "P1" : task.priority === "medium" ? "P2" : "P3"}
        </span>
        {task.kind === "idea" && (
          <span className={`text-[9px] px-1 rounded ${KIND_BADGES[task.kind] || ""}`}>
            Idea
          </span>
        )}
        {task.tags.length > 0 && (
          <span className="text-[9px] text-pir-text-muted">{task.tags[0]}</span>
        )}
      </div>

      {/* Quick action buttons — visible on hover */}
      {showActions && !isReview && (
        <div className="absolute top-1.5 right-1.5 flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
          {nextStatus && (
            <button
              title={`Move to ${nextStatus}`}
              disabled={isInflight}
              onClick={(e) => {
                e.stopPropagation();
                onQuickAction(task.id, nextStatus);
              }}
              className="w-6 h-6 flex items-center justify-center rounded bg-pir-accent/20 text-pir-accent hover:bg-pir-accent/40 disabled:opacity-30 text-[11px]"
            >
              →
            </button>
          )}
          <button
            title="Reject"
            disabled={isInflight}
            onClick={(e) => {
              e.stopPropagation();
              onQuickAction(task.id, "rejected");
            }}
            className="w-6 h-6 flex items-center justify-center rounded bg-pir-error/15 text-pir-error hover:bg-pir-error/30 disabled:opacity-30 text-[11px]"
          >
            ✕
          </button>
        </div>
      )}

      {/* Review card actions — always visible */}
      {isReview && (
        <div className="mt-2 pt-1.5 border-t border-purple-500/20">
          {/* PR title */}
          {prTitle && (
            <div className="text-[10px] text-purple-700 dark:text-purple-300 font-medium truncate mb-1 leading-tight" title={prTitle}>
              {prTitle}
            </div>
          )}
          {/* PR branch */}
          {prBranch && (
            <div className="text-[10px] text-pir-text-muted/70 font-mono truncate mb-1.5 leading-tight" title={prBranch}>
              {prBranch}
            </div>
          )}
          {/* Migration conflict warning badge */}
          {mergeOrder && (
            <div
              className="flex items-center gap-1 mb-1 px-1.5 py-0.5 rounded bg-amber-500/15 border border-amber-500/30"
              title={
                mergeOrder.canMerge
                  ? `Migration conflict: another open PR shares a migration number with this one. Merge this PR first (position ${mergeOrder.position}/${mergeOrder.total}).`
                  : `Migration conflict: merge ${mergeOrder.blockedByTitle ?? `task #${mergeOrder.blockedBy?.slice(0, 8)}`} first (position ${mergeOrder.position}/${mergeOrder.total}).`
              }
            >
              <span className="text-amber-700 dark:text-amber-400 text-[11px] leading-none">⚠</span>
              <span className="text-[10px] text-amber-700 dark:text-amber-400 font-medium">Migration conflict</span>
              <span className="text-[10px] text-amber-700 dark:text-amber-400/60 ml-auto tabular-nums">
                {mergeOrder.position}/{mergeOrder.total}
              </span>
            </div>
          )}
          {/* Diff stats + merge order badge */}
          <div className="flex items-center gap-2 mb-1.5">
            {prDiff && !prDiff.is_empty && (
              <>
                <span className="text-[10px] text-emerald-700 dark:text-emerald-400 tabular-nums">+{prDiff.stats.additions}</span>
                <span className="text-[10px] text-rose-600 dark:text-rose-400 tabular-nums">-{prDiff.stats.deletions}</span>
                <span className="text-[10px] text-pir-text-muted tabular-nums">
                  {prDiff.stats.files_changed} {prDiff.stats.files_changed === 1 ? "file" : "files"}
                </span>
              </>
            )}
            {mergeOrder && (
              <span className={`text-[10px] px-1.5 py-0.5 rounded ml-auto ${
                mergeOrder.canMerge ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-400" : "bg-zinc-500/20 text-zinc-600 dark:text-zinc-400"
              }`}>
                Merge {mergeOrder.position}/{mergeOrder.total}
              </span>
            )}
          </div>
          {task.pr_status && !prTitle && (
            <div className="text-[10px] text-purple-700 dark:text-purple-400/80 mb-1.5">
              PR: {task.pr_status}
            </div>
          )}
          <div className="flex gap-1.5">
            <button
              disabled={isInflight || mergeOrder?.canMerge === false}
              title={mergeOrder?.canMerge === false
                ? `Merge ${mergeOrder.blockedByTitle ?? `task #${mergeOrder.blockedBy?.slice(0, 8)}`} first`
                : undefined}
              onClick={(e) => {
                e.stopPropagation();
                onMerge?.(task.id);
              }}
              className="px-2 py-0.5 text-[10px] bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 rounded hover:bg-emerald-500/25 disabled:opacity-30"
            >
              {task.pr_status ? "Merge" : "Complete"}
            </button>
            <button
              disabled={isInflight}
              onClick={(e) => {
                e.stopPropagation();
                onClose?.(task.id);
              }}
              className="px-2 py-0.5 text-[10px] bg-rose-500/15 text-rose-600 dark:text-rose-400 rounded hover:bg-rose-500/25 disabled:opacity-30"
            >
              Close
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onClick(task);
              }}
              className="px-2 py-0.5 text-[10px] bg-pir-accent/15 text-pir-accent rounded hover:bg-pir-accent/25"
            >
              View
            </button>
          </div>
        </div>
      )}
    </div>
  );
});

// --- Static Card (completed/rejected — no drag) ---

function StaticCard({
  task,
  onClick,
  onRevert,
}: {
  task: TaskResponse;
  onClick: (task: TaskResponse) => void;
  onRevert?: (taskId: string) => void;
}) {
  const borderColor = task.delegation ? DELEGATION_COLORS[task.delegation] || "" : "border-l-pir-text-muted/30";
  return (
    <div
      onClick={() => onClick(task)}
      className={`bg-pir-surface-1 border border-pir rounded px-3 py-2 cursor-pointer border-l-2 ${borderColor} hover:border-pir-accent transition-colors opacity-60 group relative`}
    >
      <div className="text-label text-pir-text-primary truncate">{task.title}</div>
      <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
        <span className="text-[9px] px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-400 font-medium truncate max-w-[80px]">
          {task.project}
        </span>
        <span
          className="text-[9px] text-pir-text-muted/50 tabular-nums cursor-pointer hover:text-pir-text-muted"
          title="Click to copy full task ID"
          onClick={(e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(task.id);
          }}
        >
          #{task.id.slice(0, 8)}
        </span>
      </div>
      {onRevert && (
        <button
          title="Revert this PR"
          onClick={(e) => {
            e.stopPropagation();
            onRevert(task.id);
          }}
          className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity px-1.5 py-0.5 text-[9px] bg-amber-500/15 text-amber-700 dark:text-amber-400 rounded hover:bg-amber-500/25"
        >
          ↩ Revert
        </button>
      )}
    </div>
  );
}

// --- Drag Overlay ---

function TriageCardOverlay({ task }: { task: TaskResponse }) {
  const borderColor = task.delegation ? DELEGATION_COLORS[task.delegation] || "" : "";
  return (
    <div className={`bg-pir-surface-1 border border-pir-accent rounded px-3 py-2 shadow-lg border-l-2 ${borderColor}`}>
      <div className="text-label text-pir-text-primary truncate">{task.title}</div>
      <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
        <span className="text-[9px] px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-400 font-medium truncate max-w-[80px]">
          {task.project}
        </span>
        <span className="text-[9px] text-pir-text-muted/50 tabular-nums">
          #{task.id.slice(0, 8)}
        </span>
      </div>
    </div>
  );
}

// --- Theme v2 helpers (gated by .theme-v2) --------------------------------
//
// Pixel-close port of ui_kit target `triage-v1-conservativa.html`.
//   - Column header: dot (pe/ap/ip/rv/cp/rj) + mono uppercase label + cnt
//   - Review column: dashed purple border + purple tint bg
//   - Static columns: collapsed vertical writing-mode
//   - Cards: border-left 2px by delegation; desc w/ pr/at prefix icons;
//     badges ICE/delegation/proj/priority; quick actions on hover
//   - Review cards: pr-title purple + pr-branch mono + conflict pill + stats
//     + MERGE badge + merge/close/view actions inline
// Logic is untouched — only markup/typography changes.

const STATUS_DOT_V2: Record<AnyStatus, string> = {
  pending: "bg-pir-warning",
  approved: "bg-pir-accent",
  in_progress: "bg-pir-success",
  review: "bg-[#a78bfa]",
  completed: "bg-pir-success",
  failed: "bg-pir-error",
  rejected: "bg-pir-text-muted/60",
};

// inline-style border-left color per delegation (matches target CSS vars).
const DELEGATION_BORDER_V2: Record<string, string> = {
  agent: "hsl(var(--pir-success))",
  hybrid: "hsl(210 80% 55%)",
  human: "hsl(var(--pir-warning))",
};

function V2ColHeader({
  status,
  label,
  count,
  collapsed,
}: {
  status: AnyStatus;
  label: string;
  count: number;
  collapsed?: boolean;
}) {
  const vertical: React.CSSProperties = collapsed
    ? { writingMode: "vertical-rl", textOrientation: "mixed", padding: "8px 0" }
    : {};
  return (
    <h3
      className="text-pir-text-tertiary uppercase flex items-center m-0"
      style={{
        fontFamily: "var(--pir-font-mono)",
        fontWeight: 600,
        fontSize: "10.5px",
        lineHeight: 1,
        letterSpacing: "0.18em",
        padding: collapsed ? undefined : "4px 4px 8px",
        gap: "7px",
        ...vertical,
      }}
    >
      <span
        aria-hidden
        className={`shrink-0 rounded-full ${STATUS_DOT_V2[status]}`}
        style={{ width: 7, height: 7 }}
      />
      <span>{label}</span>
      <span
        className="text-pir-text-muted"
        style={{
          fontWeight: 500,
          letterSpacing: "0.02em",
          marginLeft: collapsed ? 0 : "auto",
        }}
      >
        {count}
      </span>
    </h3>
  );
}

function V2DroppableColumn({
  id,
  count,
  children,
}: {
  id: Column;
  count: number;
  children: React.ReactNode;
}) {
  const isReviewCol = id === "review";
  const { setNodeRef, isOver } = useDroppable({ id, disabled: isReviewCol });
  const baseStyle: React.CSSProperties = {
    flex: 1,
    minWidth: 240,
    maxWidth: 320,
    padding: 8,
    display: "flex",
    flexDirection: "column",
    gap: 6,
  };
  const reviewStyle: React.CSSProperties = isReviewCol
    ? {
        border: "1px dashed hsl(280 70% 60% / 0.35)",
        background: "hsl(280 70% 60% / 0.03)",
      }
    : { background: "hsl(var(--pir-surface-0))" };
  return (
    <div
      ref={setNodeRef}
      className={`rounded transition-colors ${isOver && !isReviewCol ? "ring-1 ring-pir-accent" : ""}`}
      style={{ ...baseStyle, ...reviewStyle, borderRadius: 4 }}
    >
      <V2ColHeader status={id} label={COLUMN_LABELS[id]} count={count} />
      <div className="flex flex-col gap-[6px]" style={{ minHeight: 60 }}>
        {children}
      </div>
    </div>
  );
}

function V2StaticColumn({
  id,
  count,
  collapsed,
  onToggle,
  children,
}: {
  id: AnyStatus;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  const baseStyle: React.CSSProperties = {
    padding: 8,
    display: "flex",
    flexDirection: "column",
    gap: 6,
    opacity: 0.75,
    background: "hsl(var(--pir-surface-0))",
    borderRadius: 4,
  };
  const geomStyle: React.CSSProperties = collapsed
    ? { minWidth: 48, maxWidth: 48 }
    : { flex: 1, minWidth: 240, maxWidth: 320 };
  return (
    <div
      onClick={onToggle}
      className="cursor-pointer select-none"
      style={{ ...baseStyle, ...geomStyle, alignItems: collapsed ? "center" : undefined }}
    >
      <V2ColHeader status={id} label={COLUMN_LABELS[id]} count={count} collapsed={collapsed} />
      {!collapsed && (
        <div className="flex flex-col gap-[6px]" onClick={(e) => e.stopPropagation()} style={{ minHeight: 60 }}>
          {children}
        </div>
      )}
    </div>
  );
}

// v2 priority label + badge class (extracted to avoid nested ternaries).
const PRIORITY_LABEL_V2: Record<string, string> = {
  high: "P1",
  medium: "P2",
  low: "P3",
};

const PRIORITY_BADGE_V2: Record<string, string> = {
  high: "bg-pir-error/12 text-pir-error",
  medium: "bg-pir-warning/12 text-pir-warning",
  low: "bg-pir-text-muted/30 text-pir-text-muted",
};

const DELEGATION_BADGE_V2: Record<string, string> = {
  agent: "bg-pir-success/15 text-pir-success",
  hybrid: "bg-[hsl(210_80%_55%/0.15)] text-[hsl(210_80%_60%)]",
  human: "bg-pir-warning/15 text-pir-warning",
};

function resolveDragOpacity(isDragging: boolean, isInflight: boolean): number {
  if (isDragging) return 0.4;
  if (isInflight) return 0.6;
  return 1;
}

function mergeConflictMessage(order: MergeOrderInfo): string {
  const blockedLabel =
    order.blockedByTitle ?? `task #${order.blockedBy?.slice(0, 8) ?? ""}`;
  if (order.canMerge) {
    return `Migration conflict: another open PR shares a migration number with this one. Merge this PR first (position ${order.position}/${order.total}).`;
  }
  return `Migration conflict: merge ${blockedLabel} first (position ${order.position}/${order.total}).`;
}

function mergeBlockedTooltip(order: MergeOrderInfo): string {
  const blockedLabel =
    order.blockedByTitle ?? `task #${order.blockedBy?.slice(0, 8) ?? ""}`;
  return `Merge ${blockedLabel} first`;
}

// Review-only inline block (PR title/branch + migration conflict pill + diff
// stats) — extracted from TriageCardV2 to keep its cognitive complexity low.
function ReviewInlineV2({
  prTitle,
  prBranch,
  mergeOrder,
  prDiff,
}: {
  prTitle?: string | null;
  prBranch?: string | null;
  mergeOrder?: MergeOrderInfo | null;
  prDiff?: PrDiff | null;
}) {
  const hasStats = Boolean((prDiff && !prDiff.is_empty) || mergeOrder);
  return (
    <>
      {prTitle && (
        <div
          style={{
            fontFamily: "var(--pir-font-sans)",
            fontWeight: 500,
            fontSize: "10px",
            lineHeight: 1.35,
            color: "hsl(280 70% 65%)",
          }}
          title={prTitle}
          className="truncate"
        >
          {prTitle}
        </div>
      )}
      {prBranch && (
        <div
          className="text-pir-text-muted truncate"
          style={{
            fontFamily: "var(--pir-font-mono)",
            fontWeight: 500,
            fontSize: "9.5px",
            lineHeight: 1.2,
          }}
          title={prBranch}
        >
          {prBranch}
        </div>
      )}
      {mergeOrder && (
        <div
          className="inline-flex items-center text-pir-warning"
          style={{
            gap: 5,
            padding: "3px 6px",
            borderRadius: 2,
            background: "hsl(var(--pir-warning) / 0.12)",
            border: "1px solid hsl(var(--pir-warning) / 0.25)",
            fontFamily: "var(--pir-font-sans)",
            fontWeight: 500,
            fontSize: "10px",
            lineHeight: 1,
          }}
          title={mergeConflictMessage(mergeOrder)}
        >
          <span aria-hidden>⚠</span>
          <span>Migration conflict</span>
          <span
            style={{
              marginLeft: "auto",
              fontFamily: "var(--pir-font-mono)",
              opacity: 0.7,
              fontSize: "9.5px",
            }}
          >
            {mergeOrder.position}/{mergeOrder.total}
          </span>
        </div>
      )}
      {hasStats && (
        <div
          className="flex items-center"
          style={{
            gap: 8,
            fontFamily: "var(--pir-font-mono)",
            fontWeight: 500,
            fontSize: "10px",
            lineHeight: 1,
          }}
        >
          {prDiff && !prDiff.is_empty && (
            <>
              <span className="text-pir-success">+{prDiff.stats.additions}</span>
              <span className="text-pir-error">
                {"−"}
                {prDiff.stats.deletions}
              </span>
              <span className="text-pir-text-muted">
                {prDiff.stats.files_changed}{" "}
                {prDiff.stats.files_changed === 1 ? "file" : "files"}
              </span>
            </>
          )}
          {mergeOrder && (
            <span
              className={
                mergeOrder.canMerge
                  ? "bg-pir-success/15 text-pir-success"
                  : "bg-pir-text-muted/20 text-pir-text-tertiary"
              }
              style={{
                marginLeft: "auto",
                padding: "2px 6px",
                borderRadius: 2,
                fontSize: "9.5px",
                letterSpacing: "0.05em",
              }}
            >
              MERGE {mergeOrder.position}/{mergeOrder.total}
            </span>
          )}
        </div>
      )}
    </>
  );
}

// Review action row (Merge/Close/View) — also extracted to keep TriageCardV2
// cognitive complexity under the gate threshold.
function ReviewActionsV2({
  task,
  isInflight,
  mergeOrder,
  onMerge,
  onClose,
  onClick,
}: {
  task: TaskResponse;
  isInflight: boolean;
  mergeOrder?: MergeOrderInfo | null;
  onMerge?: (taskId: string) => void;
  onClose?: (taskId: string) => void;
  onClick: (task: TaskResponse) => void;
}) {
  const mergeBlocked = mergeOrder?.canMerge === false;
  const btnStyle: React.CSSProperties = {
    fontFamily: "var(--pir-font-mono)",
    fontWeight: 600,
    fontSize: "10px",
    lineHeight: 1,
    letterSpacing: "0.04em",
    padding: "5px 9px",
    borderRadius: 2,
  };
  return (
    <div className="flex items-center" style={{ gap: 4, paddingTop: 4 }}>
      <button
        type="button"
        disabled={isInflight || mergeBlocked}
        title={mergeBlocked && mergeOrder ? mergeBlockedTooltip(mergeOrder) : undefined}
        onClick={(e) => {
          e.stopPropagation();
          onMerge?.(task.id);
        }}
        className="border-0 cursor-pointer bg-pir-success/15 text-pir-success hover:bg-pir-success/28 disabled:opacity-40 disabled:cursor-not-allowed"
        style={btnStyle}
      >
        {task.pr_status ? "Merge" : "Complete"}
      </button>
      <button
        type="button"
        disabled={isInflight}
        onClick={(e) => {
          e.stopPropagation();
          onClose?.(task.id);
        }}
        className="border-0 cursor-pointer bg-pir-error/13 text-pir-error hover:bg-pir-error/25 disabled:opacity-30"
        style={btnStyle}
      >
        Close
      </button>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onClick(task);
        }}
        className="border-0 cursor-pointer bg-pir-accent/13 text-pir-accent hover:bg-pir-accent/25"
        style={{ ...btnStyle, marginLeft: "auto" }}
      >
        View
      </button>
    </div>
  );
}

// Quick action cluster for non-review cards (→ next / ✕ reject on hover).
function QuickActionsV2({
  task,
  isInflight,
  nextStatus,
  onQuickAction,
}: {
  task: TaskResponse;
  isInflight: boolean;
  nextStatus: string | undefined;
  onQuickAction: (taskId: string, newStatus: string) => void;
}) {
  const qbStyle: React.CSSProperties = {
    width: 22,
    height: 22,
    borderRadius: 3,
    fontSize: 12,
  };
  return (
    <div
      className="absolute flex opacity-0 group-hover:opacity-100 transition-opacity"
      style={{ top: 6, right: 6, gap: 3 }}
    >
      {nextStatus && (
        <button
          type="button"
          title={`Move to ${nextStatus}`}
          disabled={isInflight}
          onClick={(e) => {
            e.stopPropagation();
            onQuickAction(task.id, nextStatus);
          }}
          className="inline-flex items-center justify-center border-0 cursor-pointer bg-pir-accent/20 text-pir-accent hover:bg-pir-accent/35 disabled:opacity-30"
          style={qbStyle}
        >
          →
        </button>
      )}
      <button
        type="button"
        title="Reject"
        disabled={isInflight}
        onClick={(e) => {
          e.stopPropagation();
          onQuickAction(task.id, "rejected");
        }}
        className="inline-flex items-center justify-center border-0 cursor-pointer bg-pir-error/15 text-pir-error hover:bg-pir-error/30 disabled:opacity-30"
        style={qbStyle}
      >
        ✕
      </button>
    </div>
  );
}

function BadgeV2({
  kind,
  children,
  bold = true,
  style: extraStyle,
}: {
  kind: string;
  children: React.ReactNode;
  bold?: boolean;
  style?: React.CSSProperties;
}) {
  return (
    <span
      className={`inline-flex items-center rounded-sm ${kind}`}
      style={{
        fontFamily: "var(--pir-font-mono)",
        fontWeight: bold ? 600 : 500,
        fontSize: 9,
        lineHeight: 1,
        padding: "2px 5px",
        letterSpacing: "0.03em",
        ...extraStyle,
      }}
    >
      {children}
    </span>
  );
}

const TriageCardV2 = memo(function TriageCardV2({
  task,
  isInflight,
  onClick,
  onQuickAction,
  onMerge,
  onClose,
  showActions,
  prDiff,
  mergeOrder,
  prTitle,
  prBranch,
}: {
  task: TaskResponse;
  isInflight: boolean;
  onClick: (task: TaskResponse) => void;
  onQuickAction: (taskId: string, newStatus: string) => void;
  onMerge?: (taskId: string) => void;
  onClose?: (taskId: string) => void;
  showActions: boolean;
  prDiff?: PrDiff | null;
  mergeOrder?: MergeOrderInfo | null;
  prTitle?: string | null;
  prBranch?: string | null;
}) {
  const isReview = task.status === "review";
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: task.id,
    disabled: isInflight || isReview,
  });

  const borderLeftColor = task.delegation
    ? DELEGATION_BORDER_V2[task.delegation] ?? "var(--pir-text-muted)"
    : "var(--pir-text-muted)";
  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: resolveDragOpacity(isDragging, isInflight),
    display: "flex",
    flexDirection: "column",
    gap: "6px",
    padding: "9px 10px",
    borderRadius: 2,
    border: `1px solid ${isReview ? "hsl(280 70% 60% / 0.2)" : "var(--pir-border)"}`,
    borderLeftWidth: 2,
    borderLeftStyle: "solid",
    borderLeftColor,
    background: isReview ? "hsl(280 70% 60% / 0.04)" : "hsl(var(--pir-surface-1))",
    cursor: isReview ? "default" : "grab",
    position: "relative",
    transitionProperty: "border-color",
    transitionDuration: "120ms",
  };

  const parsedDesc = task.description ? parseDescription(task.description) : null;
  const nextStatus = NEXT_STATUS[task.status];
  const priorityLabel = PRIORITY_LABEL_V2[task.priority] ?? "P3";
  const priorityClass = PRIORITY_BADGE_V2[task.priority] ?? PRIORITY_BADGE_V2.low;

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      onClick={() => !isDragging && onClick(task)}
      className="group hover:border-pir-strong"
    >
      {/* Title */}
      <div
        className="text-pir-text-primary"
        style={{
          fontFamily: "var(--pir-font-sans)",
          fontWeight: 500,
          fontSize: "12.5px",
          lineHeight: 1.35,
          paddingRight: isReview ? 0 : 48,
        }}
      >
        {task.title}
      </div>

      {/* Meta */}
      <div
        className="flex items-center gap-[6px] text-pir-text-muted"
        style={{
          fontFamily: "var(--pir-font-mono)",
          fontWeight: 400,
          fontSize: "9.5px",
          lineHeight: 1,
        }}
      >
        <span
          className="cursor-pointer hover:text-pir-text-secondary"
          title="Click to copy full task ID"
          onClick={(e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(task.id);
          }}
        >
          #{task.id.slice(0, 8)}
        </span>
        {task.created_by && (
          <span style={{ opacity: 0.75 }}>by {task.created_by}</span>
        )}
      </div>

      {/* Description (problem + attention) */}
      {(parsedDesc?.problem || parsedDesc?.attention) && (
        <div
          className="flex flex-col gap-[2px]"
          style={{
            paddingTop: 5,
            borderTop: "1px solid hsl(var(--pir-border) / 0.6)",
            fontFamily: "var(--pir-font-sans)",
            fontSize: "10.5px",
            lineHeight: 1.35,
            fontWeight: 400,
          }}
        >
          {parsedDesc?.problem && (
            <div className="text-pir-error flex gap-[4px]">
              <span aria-hidden style={{ opacity: 0.65, fontWeight: 700 }}>
                !
              </span>
              <span className="line-clamp-2">{parsedDesc.problem}</span>
            </div>
          )}
          {parsedDesc?.attention && (
            <div className="text-pir-warning flex gap-[4px] items-center">
              <span aria-hidden style={{ opacity: 0.65, fontSize: 8 }}>
                ◆
              </span>
              <span className="line-clamp-2">{parsedDesc.attention}</span>
            </div>
          )}
        </div>
      )}

      {/* Review-only content (PR title, branch, stats, conflict) */}
      {isReview && (
        <ReviewInlineV2
          prTitle={prTitle}
          prBranch={prBranch}
          mergeOrder={mergeOrder}
          prDiff={prDiff}
        />
      )}

      {/* Badges */}
      <div className="flex flex-wrap items-center" style={{ gap: "3px" }}>
        {task.ice_score != null && (
          <BadgeV2 kind="bg-pir-accent/15 text-pir-accent">
            <span style={{ fontWeight: 700 }}>{task.ice_score}</span>
          </BadgeV2>
        )}
        {!isReview && task.delegation && (
          <BadgeV2 kind={DELEGATION_BADGE_V2[task.delegation] ?? ""}>
            {task.delegation}
          </BadgeV2>
        )}
        <BadgeV2
          kind="bg-[hsl(195_70%_55%/0.15)] text-[hsl(195_70%_60%)]"
          bold={false}
          style={{ maxWidth: 90, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
        >
          {task.project}
        </BadgeV2>
        <BadgeV2 kind={priorityClass}>
          <span style={{ fontWeight: 700 }}>{priorityLabel}</span>
        </BadgeV2>
        {task.kind === "idea" && (
          <BadgeV2 kind="bg-[hsl(290_70%_55%/0.15)] text-[hsl(290_70%_60%)]" bold={false}>
            Idea
          </BadgeV2>
        )}
        {task.tags.length > 0 && !isReview && (
          <span
            className="text-pir-text-muted"
            style={{
              fontFamily: "var(--pir-font-mono)",
              fontWeight: 500,
              fontSize: 9,
              padding: "2px 0",
            }}
          >
            {task.tags[0]}
          </span>
        )}
      </div>

      {/* Quick action buttons — visible on hover */}
      {showActions && !isReview && (
        <QuickActionsV2
          task={task}
          isInflight={isInflight}
          nextStatus={nextStatus}
          onQuickAction={onQuickAction}
        />
      )}

      {/* Review actions (Merge/Close/View) */}
      {isReview && (
        <ReviewActionsV2
          task={task}
          isInflight={isInflight}
          mergeOrder={mergeOrder}
          onMerge={onMerge}
          onClose={onClose}
          onClick={onClick}
        />
      )}
    </div>
  );
});

function StaticCardV2({
  task,
  onClick,
  onRevert,
}: {
  task: TaskResponse;
  onClick: (task: TaskResponse) => void;
  onRevert?: (taskId: string) => void;
}) {
  const borderColor = task.delegation
    ? DELEGATION_BORDER_V2[task.delegation] ?? "var(--pir-text-muted)"
    : "var(--pir-text-muted)";
  return (
    <div
      onClick={() => onClick(task)}
      className="group relative cursor-pointer hover:border-pir-strong transition-colors"
      style={{
        opacity: 0.55,
        padding: "7px 9px",
        display: "flex",
        flexDirection: "column",
        gap: 3,
        background: "hsl(var(--pir-surface-1))",
        border: "1px solid var(--pir-border)",
        borderLeftWidth: 2,
        borderLeftStyle: "solid",
        borderLeftColor: borderColor,
        borderRadius: 2,
      }}
    >
      <div
        className="text-pir-text-secondary truncate"
        style={{
          fontFamily: "var(--pir-font-sans)",
          fontWeight: 500,
          fontSize: "11.5px",
          lineHeight: 1.35,
        }}
      >
        {task.title}
      </div>
      <div className="flex flex-wrap items-center" style={{ gap: 3 }}>
        <BadgeV2
          kind="bg-[hsl(195_70%_55%/0.15)] text-[hsl(195_70%_60%)]"
          bold={false}
          style={{ maxWidth: 80, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
        >
          {task.project}
        </BadgeV2>
        <span
          className="text-pir-text-muted/60 cursor-pointer hover:text-pir-text-muted"
          title="Click to copy full task ID"
          onClick={(e) => {
            e.stopPropagation();
            navigator.clipboard.writeText(task.id);
          }}
          style={{
            fontFamily: "var(--pir-font-mono)",
            fontWeight: 400,
            fontSize: 9,
          }}
        >
          #{task.id.slice(0, 8)}
        </span>
      </div>
      {onRevert && (
        <button
          type="button"
          title="Revert this PR"
          onClick={(e) => {
            e.stopPropagation();
            onRevert(task.id);
          }}
          className="absolute opacity-0 group-hover:opacity-100 transition-opacity border-0 cursor-pointer bg-pir-warning/15 text-pir-warning hover:bg-pir-warning/25"
          style={{
            top: 6,
            right: 6,
            padding: "2px 6px",
            fontFamily: "var(--pir-font-mono)",
            fontSize: 9,
            fontWeight: 600,
            borderRadius: 2,
          }}
        >
          ↩ Revert
        </button>
      )}
    </div>
  );
}

// --- Main TriageKanban ---

interface Props {
  tasks: TaskResponse[];
  onTasksChange: (updater: (prev: TaskResponse[]) => TaskResponse[]) => void;
  onTaskClick: (task: TaskResponse) => void;
}

export default function TriageKanban({ tasks, onTasksChange, onTaskClick }: Props) {
  const v2 = useDesignV2();
  const [draggedTask, setDraggedTask] = useState<TaskResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [columnPages, setColumnPages] = useState<Record<string, number>>({});
  const [rejectedCollapsed, setRejectedCollapsed] = useState(true);
  const [completedCollapsed, setCompletedCollapsed] = useState(true);
  const [prDiffMap, setPrDiffMap] = useState<Record<string, PrDiff>>({});
  const [mergeConflicts, setMergeConflicts] = useState<MergeConflictResponse | null>(null);
  const [prMetaMap, setPrMetaMap] = useState<Record<string, { title: string | null; branch: string | null }>>({});
  const inflightTasks = useRef(new Set<string>());

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } })
  );

  // Fetch diff stats for all review tasks (lazy, one-shot per task)
  const reviewTaskIds = useMemo(
    () => tasks.filter((t) => t.status === "review").map((t) => t.id),
    [tasks]
  );
  useEffect(() => {
    if (reviewTaskIds.length === 0) return;
    const ctrl = new AbortController();
    for (const taskId of reviewTaskIds) {
      if (prDiffMap[taskId]) continue;
      getPullRequest(taskId, { signal: ctrl.signal })
        .then((pr) => {
          if (pr.diff) {
            setPrDiffMap((prev) => ({ ...prev, [taskId]: pr.diff! }));
          }
          setPrMetaMap((prev) => ({ ...prev, [taskId]: { title: pr.title ?? null, branch: pr.branch ?? null } }));
        })
        .catch(() => {});
    }
    return () => ctrl.abort();
  }, [reviewTaskIds.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch merge conflicts for all distinct projects with review tasks
  const reviewProjects = useMemo(
    () => [...new Set(tasks.filter((t) => t.status === "review").map((t) => t.project))],
    [tasks]
  );
  const fetchMergeConflicts = useCallback(async () => {
    if (reviewProjects.length === 0) {
      setMergeConflicts(null);
      return;
    }
    // Fetch conflicts for each project and merge results
    const allConflicts: MergeConflictResponse["conflicts"] = [];
    for (const project of reviewProjects) {
      try {
        const resp = await getMergeConflicts(project);
        allConflicts.push(...resp.conflicts);
      } catch {
        // Silently ignore — non-critical feature
      }
    }
    setMergeConflicts({ conflicts: allConflicts });
  }, [reviewProjects.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    fetchMergeConflicts();
  }, [fetchMergeConflicts]);

  // Build lookup map: taskId -> MergeOrderInfo
  const mergeOrderMap = useMemo(() => {
    const map = new Map<string, MergeOrderInfo>();
    if (!mergeConflicts) return map;
    for (const group of mergeConflicts.conflicts) {
      const total = group.tasks.length;
      for (const entry of group.tasks) {
        // Find the title of the blocking task for tooltip
        const blockedByTitle = entry.blocked_by
          ? tasks.find((t) => t.id === entry.blocked_by)?.title ?? null
          : null;
        map.set(entry.task_id, {
          position: entry.merge_position,
          total,
          canMerge: entry.can_merge,
          blockedBy: entry.blocked_by,
          blockedByTitle,
        });
      }
    }
    return map;
  }, [mergeConflicts, tasks]);

  const tasksByColumn = useMemo(() => {
    const result: Record<string, TaskResponse[]> = {
      pending: [],
      approved: [],
      in_progress: [],
      review: [],
      completed: [],
      rejected: [],
    };
    for (const task of tasks) {
      if (task.status in result) {
        result[task.status].push(task);
      }
    }
    return result;
  }, [tasks]);

  const handleDragStart = useCallback(
    (event: DragStartEvent) => {
      const task = tasks.find((t) => t.id === event.active.id);
      setDraggedTask(task || null);
    },
    [tasks]
  );

  const handleDragEnd = useCallback(
    async (event: DragEndEvent) => {
      setDraggedTask(null);
      const { active, over } = event;
      if (!over) return;

      const taskId = active.id as string;
      let newStatus = over.id as string;

      // If dropped on a task, use that task's column
      if (!COLUMNS.includes(newStatus as Column)) {
        const overTask = tasks.find((t) => t.id === over.id);
        if (overTask) newStatus = overTask.status;
      }

      const task = tasks.find((t) => t.id === taskId);
      if (!task || task.status === newStatus) return;

      // Block manual drag into review — only PR submit can set this status
      if (newStatus === "review") return;

      await doStatusChange(taskId, newStatus);
    },
    [tasks, onTasksChange]
  );

  // Shared status change logic for drag-drop and quick actions
  const doStatusChange = useCallback(
    async (taskId: string, newStatus: string) => {
      if (inflightTasks.current.has(taskId)) return;

      inflightTasks.current.add(taskId);
      const prevTasks = [...tasks];
      onTasksChange((prev) =>
        prev.map((t) => (t.id === taskId ? { ...t, status: newStatus as TaskResponse["status"] } : t))
      );
      setError(null);

      try {
        await updateTask(taskId, { status: newStatus as TaskResponse["status"] });
      } catch (err) {
        onTasksChange(() => prevTasks);
        setError(err instanceof Error ? err.message : "Failed to update task status");
        setTimeout(() => setError(null), 4000);
      } finally {
        inflightTasks.current.delete(taskId);
      }
    },
    [tasks, onTasksChange]
  );

  const handleQuickAction = useCallback(
    (taskId: string, newStatus: string) => {
      doStatusChange(taskId, newStatus);
    },
    [doStatusChange]
  );

  const handleMerge = useCallback(
    async (taskId: string) => {
      const task = tasks.find((t) => t.id === taskId);
      // No PR — skip merge, go directly to completed
      if (!task?.pr_status) {
        await doStatusChange(taskId, "completed");
        return;
      }
      if (inflightTasks.current.has(taskId)) return;
      inflightTasks.current.add(taskId);
      setError(null);
      try {
        await mergePullRequest(taskId);
        // Task will transition to completed via backend auto-transition
        onTasksChange((prev) =>
          prev.map((t) => (t.id === taskId ? { ...t, status: "completed" as TaskResponse["status"] } : t))
        );
        // Re-fetch merge conflicts — the merged PR is gone, next in line becomes mergeable
        fetchMergeConflicts();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to merge PR");
        setTimeout(() => setError(null), 4000);
      } finally {
        inflightTasks.current.delete(taskId);
      }
    },
    [tasks, onTasksChange, doStatusChange, fetchMergeConflicts]
  );

  const handleClose = useCallback(
    async (taskId: string) => {
      if (inflightTasks.current.has(taskId)) return;
      inflightTasks.current.add(taskId);
      setError(null);
      try {
        await closePullRequest(taskId, "Closed from Triage");
        // Task will transition to in_progress via backend auto-transition
        onTasksChange((prev) =>
          prev.map((t) => (t.id === taskId ? { ...t, status: "in_progress" as TaskResponse["status"] } : t))
        );
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to close PR");
        setTimeout(() => setError(null), 4000);
      } finally {
        inflightTasks.current.delete(taskId);
      }
    },
    [onTasksChange]
  );

  const handleRevert = useCallback(
    async (taskId: string) => {
      if (inflightTasks.current.has(taskId)) return;
      inflightTasks.current.add(taskId);
      setError(null);
      try {
        const result = await revertPullRequest(taskId);
        // Add the new revert task to the board (in_progress + open PR = review)
        onTasksChange((prev) => {
          const origTask = prev.find((t) => t.id === taskId);
          const revertTask: TaskResponse = {
            id: result.revert_task_id,
            title: `Revert: ${origTask?.title ?? ""}`,
            kind: "normal",
            status: "review",
            project: origTask?.project ?? "",
            priority: "high",
            delegation: "human",
            tags: [],
            pr_status: "open",
            ice_score: null,
            description: null,
            created_by: "",
            owner_id: null,
            owner: null,
            source: "manual",
            source_ref: null,
            deleted_at: null,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            impact: null,
            confidence: null,
            ease: null,
            scored_by: null,
            scored_at: null,
            review_feedback: null,
          };
          return [...prev, revertTask];
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to revert PR");
        setTimeout(() => setError(null), 4000);
      } finally {
        inflightTasks.current.delete(taskId);
      }
    },
    [tasks, onTasksChange]
  );

  const completedTasks = tasksByColumn["completed"] || [];
  const rejectedTasks = tasksByColumn["rejected"] || [];

  // v2 component swap — identical handlers, different markup/typography
  const Card = v2 ? TriageCardV2 : TriageCard;
  const DropCol = v2 ? V2DroppableColumn : DroppableColumn;
  const StaticCol = v2 ? V2StaticColumn : StaticColumn;
  const StaticCardComp = v2 ? StaticCardV2 : StaticCard;

  return (
    <div className={v2 ? "" : "space-y-2"}>
      {/* Error toast */}
      {error && (
        <ErrorAlert message={error} />
      )}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCorners}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <div
          className={v2 ? "flex overflow-x-auto items-start" : "flex gap-2 overflow-x-auto pb-2"}
          style={v2 ? { gap: 10 } : undefined}
        >
          {/* Active columns (droppable) */}
          {COLUMNS.map((col) => {
            const colTasks = tasksByColumn[col] || [];
            const page = columnPages[col] || 1;
            const visible = colTasks.slice(0, page * PAGE_SIZE);
            const hasMore = visible.length < colTasks.length;

            return (
              <DropCol key={col} id={col} count={colTasks.length}>
                <SortableContext
                  items={visible.map((t) => t.id)}
                  strategy={verticalListSortingStrategy}
                >
                  {visible.map((task) => (
                    <Card
                      key={task.id}
                      task={task}
                      isInflight={inflightTasks.current.has(task.id)}
                      onClick={onTaskClick}
                      onQuickAction={handleQuickAction}
                      onMerge={handleMerge}
                      onClose={handleClose}
                      showActions
                      prDiff={task.status === "review" ? (prDiffMap[task.id] ?? null) : null}
                      mergeOrder={task.status === "review" ? (mergeOrderMap.get(task.id) ?? null) : null}
                      prTitle={task.status === "review" ? (prMetaMap[task.id]?.title ?? null) : null}
                      prBranch={task.status === "review" ? (prMetaMap[task.id]?.branch ?? null) : null}
                    />
                  ))}
                </SortableContext>
                {hasMore && (
                  <button
                    onClick={() =>
                      setColumnPages((prev) => ({ ...prev, [col]: (prev[col] || 1) + 1 }))
                    }
                    className="w-full text-caption text-pir-text-muted hover:text-pir-text-secondary py-2 transition-colors"
                  >
                    Show more ({colTasks.length - visible.length} remaining)
                  </button>
                )}
              </DropCol>
            );
          })}

          {/* Completed column (static, collapsible) */}
          <StaticCol
            id="completed"
            count={completedTasks.length}
            collapsed={completedCollapsed}
            onToggle={() => setCompletedCollapsed((v) => !v)}
          >
            {completedTasks.slice(0, (columnPages["completed"] || 1) * PAGE_SIZE).map((task) => (
              <StaticCardComp key={task.id} task={task} onClick={onTaskClick} onRevert={handleRevert} />
            ))}
            {completedTasks.length > (columnPages["completed"] || 1) * PAGE_SIZE && (
              <button
                onClick={() =>
                  setColumnPages((prev) => ({ ...prev, completed: (prev["completed"] || 1) + 1 }))
                }
                className="w-full text-caption text-pir-text-muted hover:text-pir-text-secondary py-2 transition-colors"
              >
                Show more ({completedTasks.length - (columnPages["completed"] || 1) * PAGE_SIZE} remaining)
              </button>
            )}
          </StaticCol>

          {/* Rejected column (static, collapsed by default) */}
          {rejectedTasks.length > 0 && (
            <StaticCol
              id="rejected"
              count={rejectedTasks.length}
              collapsed={rejectedCollapsed}
              onToggle={() => setRejectedCollapsed((v) => !v)}
            >
              {rejectedTasks.slice(0, (columnPages["rejected"] || 1) * PAGE_SIZE).map((task) => (
                <StaticCardComp key={task.id} task={task} onClick={onTaskClick} />
              ))}
              {rejectedTasks.length > (columnPages["rejected"] || 1) * PAGE_SIZE && (
                <button
                  onClick={() =>
                    setColumnPages((prev) => ({ ...prev, rejected: (prev["rejected"] || 1) + 1 }))
                  }
                  className="w-full text-caption text-pir-text-muted hover:text-pir-text-secondary py-2 transition-colors"
                >
                  Show more ({rejectedTasks.length - (columnPages["rejected"] || 1) * PAGE_SIZE} remaining)
                </button>
              )}
            </StaticCol>
          )}
        </div>
        <DragOverlay>
          {draggedTask && <TriageCardOverlay task={draggedTask} />}
        </DragOverlay>
      </DndContext>
    </div>
  );
}
