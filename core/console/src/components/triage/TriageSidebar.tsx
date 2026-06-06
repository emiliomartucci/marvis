"use client";

// v1.1.0 - 2026-04-22 - theme-v2 polish: dot markers + chip variants + grp-lbl (gated)
import { filterTriageTasks } from "@/lib/triage";
import { useDesignV2 } from "@/lib/useDesignV2";
import type { TaskResponse, TriageFilters, TaskStatus, TaskPriority, DelegationType, TaskKind } from "@/lib/types";

const STATUS_OPTIONS: { value: TaskStatus; label: string; color: string }[] = [
  { value: "pending", label: "Pending", color: "bg-pir-warning" },
  { value: "approved", label: "Approved", color: "bg-pir-accent" },
  { value: "in_progress", label: "In Progress", color: "bg-pir-success" },
  { value: "review", label: "Review", color: "bg-purple-500" },
];

// v2 dot classes map 1:1 with target .fl-row .dot.pe/ap/ip/rv (purple-400 ~ #a78bfa).
const STATUS_DOT_V2: Record<TaskStatus, string> = {
  pending: "bg-pir-warning",
  approved: "bg-pir-accent",
  in_progress: "bg-pir-success",
  review: "bg-[#a78bfa]",
  completed: "bg-pir-success",
  rejected: "bg-pir-text-muted/60",
  failed: "bg-pir-error",
};

const PRIORITY_OPTIONS: { value: TaskPriority; label: string }[] = [
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

const KIND_OPTIONS: { value: TaskKind; label: string; color: string }[] = [
  { value: "idea", label: "Ideas", color: "bg-fuchsia-500/20 text-fuchsia-700 dark:text-fuchsia-400" },
  { value: "normal", label: "Normal", color: "bg-pir-surface-2 text-pir-text-secondary" },
];

const DELEGATION_OPTIONS: { value: DelegationType | "unscored"; label: string; color: string }[] = [
  { value: "agent", label: "Agent", color: "bg-emerald-500/20 text-emerald-700 dark:text-emerald-400" },
  { value: "hybrid", label: "Hybrid", color: "bg-blue-500/20 text-blue-700 dark:text-blue-400" },
  { value: "human", label: "Human", color: "bg-amber-500/20 text-amber-700 dark:text-amber-400" },
  { value: "unscored", label: "Unscored", color: "bg-pir-surface-2 text-pir-text-muted" },
];

// v2-specific chip colour per target html (.chip.kind-idea / .chip.del-* / .chip.on)
const KIND_CHIP_V2: Record<TaskKind, string> = {
  idea: "border-[color:hsl(290_70%_55%/0.3)] bg-[hsl(290_70%_55%/0.15)] text-[hsl(290_70%_60%)]",
  normal: "border-pir-accent/30 bg-pir-accent/15 text-pir-accent",
};

const DELEGATION_CHIP_V2: Record<string, string> = {
  agent: "border-pir-success/30 bg-pir-success/15 text-pir-success",
  hybrid: "border-[color:hsl(210_80%_55%/0.3)] bg-[hsl(210_80%_55%/0.15)] text-[hsl(210_80%_60%)]",
  human: "border-pir-warning/30 bg-pir-warning/15 text-pir-warning",
  unscored: "border-pir-accent/30 bg-pir-accent/15 text-pir-accent",
};

interface Props {
  tasks: TaskResponse[];
  filters: TriageFilters;
  projects: string[];
  onFiltersChange: (filters: TriageFilters) => void;
}

export default function TriageSidebar(props: Props) {
  const v2 = useDesignV2();
  if (v2) return <TriageSidebarV2 {...props} />;
  return <TriageSidebarV1 {...props} />;
}

function TriageSidebarV1({ tasks, filters, projects, onFiltersChange }: Props) {
  const filteredTasks = filterTriageTasks(tasks, filters);
  const statusFacetTasks = filterTriageTasks(tasks, filters, ["status"]);
  const kindFacetTasks = filterTriageTasks(tasks, filters, ["kind"]);
  const projectFacetTasks = filterTriageTasks(tasks, filters, ["project"]);
  const total = filteredTasks.length;
  const scored = filteredTasks.filter((t) => t.ice_score != null).length;
  const unscored = total - scored;
  const ideas = filteredTasks.filter((t) => t.kind === "idea").length;

  function toggleArrayFilter<T>(current: T[], value: T): T[] {
    return current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
  }

  return (
    <div className="p-3 space-y-4">
      {/* Stats header */}
      <div>
        <div className="text-heading text-pir-text-primary">Triage</div>
        <div className="text-caption text-pir-text-muted mt-1">
          {total} tasks &middot; {ideas} ideas &middot; {scored} scored &middot; {unscored} unscored
        </div>
      </div>

      {/* Status filter */}
      <div>
        <div className="text-caption text-pir-text-tertiary mb-1.5">Status</div>
        <div className="space-y-0.5">
          {STATUS_OPTIONS.map((opt) => {
            const active = filters.status.includes(opt.value);
            const count = statusFacetTasks.filter((t) => t.status === opt.value).length;
            return (
              <button
                key={opt.value}
                onClick={() =>
                  onFiltersChange({ ...filters, status: toggleArrayFilter(filters.status, opt.value) })
                }
                className={`w-full flex items-center gap-2 px-2 py-1 rounded text-label transition-colors ${
                  active
                    ? "bg-pir-surface-2 text-pir-text-primary"
                    : "text-pir-text-secondary hover:bg-pir-surface-1"
                }`}
              >
                <span className={`w-2 h-2 rounded-full shrink-0 ${opt.color}`} />
                <span className="flex-1 text-left">{opt.label}</span>
                <span className="text-caption text-pir-text-muted">{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div>
        <div className="text-caption text-pir-text-tertiary mb-1.5">Kind</div>
        <div className="flex flex-wrap gap-1">
          {KIND_OPTIONS.map((opt) => {
            const active = filters.kind.includes(opt.value);
            const count = kindFacetTasks.filter((t) => t.kind === opt.value).length;
            return (
              <button
                key={opt.value}
                onClick={() =>
                  onFiltersChange({ ...filters, kind: toggleArrayFilter(filters.kind, opt.value) })
                }
                className={`text-[10px] px-2 py-0.5 rounded transition-colors ${
                  active ? opt.color : "bg-pir-surface-1 text-pir-text-muted hover:bg-pir-surface-2"
                }`}
              >
                {opt.label} <span className="text-pir-text-muted">{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Delegation filter */}
      <div>
        <div className="text-caption text-pir-text-tertiary mb-1.5">Delegation</div>
        <div className="flex flex-wrap gap-1">
          {DELEGATION_OPTIONS.map((opt) => {
            const active = filters.delegation.includes(opt.value);
            return (
              <button
                key={opt.value}
                onClick={() =>
                  onFiltersChange({
                    ...filters,
                    delegation: toggleArrayFilter(filters.delegation, opt.value),
                  })
                }
                className={`text-[10px] px-2 py-0.5 rounded transition-colors ${
                  active ? opt.color : "bg-pir-surface-1 text-pir-text-muted hover:bg-pir-surface-2"
                }`}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Priority filter */}
      <div>
        <div className="text-caption text-pir-text-tertiary mb-1.5">Priority</div>
        <div className="flex flex-wrap gap-1">
          {PRIORITY_OPTIONS.map((opt) => {
            const active = filters.priority.includes(opt.value);
            return (
              <button
                key={opt.value}
                onClick={() =>
                  onFiltersChange({ ...filters, priority: toggleArrayFilter(filters.priority, opt.value) })
                }
                className={`text-[10px] px-2 py-0.5 rounded transition-colors ${
                  active
                    ? "bg-pir-accent/20 text-pir-accent"
                    : "bg-pir-surface-1 text-pir-text-muted hover:bg-pir-surface-2"
                }`}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Project filter */}
      <div>
        <div className="text-caption text-pir-text-tertiary mb-1.5">Project</div>
        <div className="space-y-0.5 max-h-[200px] overflow-y-auto">
          {projects.map((p) => {
            const active = filters.project.includes(p);
            const count = projectFacetTasks.filter((t) => t.project === p).length;
            return (
              <button
                key={p}
                onClick={() =>
                  onFiltersChange({ ...filters, project: toggleArrayFilter(filters.project, p) })
                }
                className={`w-full flex items-center gap-2 px-2 py-1 rounded text-label transition-colors ${
                  active
                    ? "bg-pir-surface-2 text-pir-text-primary"
                    : "text-pir-text-secondary hover:bg-pir-surface-1"
                }`}
              >
                <span className="flex-1 text-left truncate">{p}</span>
                <span className="text-caption text-pir-text-muted">{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Clear filters */}
      {(filters.status.length > 0 ||
        filters.kind.length > 0 ||
        filters.project.length > 0 ||
        filters.priority.length > 0 ||
        filters.delegation.length > 0) && (
        <button
          onClick={() =>
            onFiltersChange({ status: [], kind: [], project: [], priority: [], delegation: [] })
          }
          className="w-full text-caption text-pir-text-muted hover:text-pir-text-secondary py-1 transition-colors"
        >
          Clear all filters
        </button>
      )}
    </div>
  );
}

// --- Theme v2 sidebar (gated by .theme-v2) --------------------------------
//
// Pixel-close port of ui_kit target `triage-v1-conservativa.html` .aside:
//   h2 title + mono meta line, .grp-lbl all-caps mono headers, .fl-row for
//   status/project (dot + label + count), .chip for kind/delegation/priority.
// Logic identical to V1 — only markup/typography differ. Gated behind
// useDesignV2() in the parent export.

function GrpLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="text-pir-text-tertiary uppercase"
      style={{
        fontFamily: "var(--pir-font-mono)",
        fontWeight: 600,
        fontSize: "9.5px",
        lineHeight: 1,
        letterSpacing: "0.22em",
        marginBottom: "6px",
      }}
    >
      {children}
    </div>
  );
}

interface FlRowProps {
  active: boolean;
  onClick: () => void;
  dotClass?: string;
  label: string;
  count?: number;
}

function FlRow({ active, onClick, dotClass, label, count }: FlRowProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full flex items-center gap-2 rounded-sm transition-colors border-0 text-left ${
        active
          ? "bg-pir-surface-2 text-pir-text-primary"
          : "bg-transparent text-pir-text-secondary hover:bg-pir-surface-1 hover:text-pir-text-primary"
      }`}
      style={{
        padding: "5px 8px",
        fontFamily: "var(--pir-font-sans)",
        fontWeight: 500,
        fontSize: "12px",
        lineHeight: 1,
      }}
    >
      {dotClass && (
        <span
          aria-hidden
          className={`shrink-0 rounded-full ${dotClass}`}
          style={{ width: 7, height: 7 }}
        />
      )}
      <span className="flex-1 truncate">{label}</span>
      {count != null && (
        <span
          className="text-pir-text-muted"
          style={{
            fontFamily: "var(--pir-font-mono)",
            fontWeight: 500,
            fontSize: "10px",
            lineHeight: 1,
          }}
        >
          {count}
        </span>
      )}
    </button>
  );
}

interface ChipV2Props {
  active: boolean;
  onClick: () => void;
  activeClass?: string;
  label: string;
  count?: number;
}

function ChipV2({ active, onClick, activeClass, label, count }: ChipV2Props) {
  const baseActive = activeClass ?? "border-pir-accent/30 bg-pir-accent/15 text-pir-accent";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center rounded-sm border transition-colors ${
        active
          ? baseActive
          : "border-transparent bg-pir-surface-1 text-pir-text-tertiary hover:bg-pir-surface-2 hover:text-pir-text-primary"
      }`}
      style={{
        padding: "4px 7px",
        fontFamily: "var(--pir-font-mono)",
        fontSize: "10px",
        fontWeight: 500,
        lineHeight: 1,
      }}
    >
      {label}
      {count != null && (
        <span
          className="text-pir-text-muted"
          style={{ marginLeft: 4, fontWeight: 400 }}
        >
          {count}
        </span>
      )}
    </button>
  );
}

function TriageSidebarV2({ tasks, filters, projects, onFiltersChange }: Props) {
  const filteredTasks = filterTriageTasks(tasks, filters);
  const statusFacetTasks = filterTriageTasks(tasks, filters, ["status"]);
  const kindFacetTasks = filterTriageTasks(tasks, filters, ["kind"]);
  const projectFacetTasks = filterTriageTasks(tasks, filters, ["project"]);
  const total = filteredTasks.length;
  const scored = filteredTasks.filter((t) => t.ice_score != null).length;
  const unscored = total - scored;
  const ideas = filteredTasks.filter((t) => t.kind === "idea").length;

  function toggleArrayFilter<T>(current: T[], value: T): T[] {
    return current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
  }

  const hasAny =
    filters.status.length > 0 ||
    filters.kind.length > 0 ||
    filters.project.length > 0 ||
    filters.priority.length > 0 ||
    filters.delegation.length > 0;

  return (
    <div
      className="flex flex-col"
      style={{ padding: "16px", gap: "18px" }}
    >
      {/* Header */}
      <div>
        <h2
          className="text-pir-text-primary m-0"
          style={{
            fontFamily: "var(--pir-font-sans)",
            fontWeight: 700,
            fontSize: "14px",
            lineHeight: 1.1,
            letterSpacing: "-0.01em",
            marginBottom: "4px",
          }}
        >
          Triage
        </h2>
        <div
          className="text-pir-text-muted"
          style={{
            fontFamily: "var(--pir-font-mono)",
            fontWeight: 500,
            fontSize: "10px",
            lineHeight: 1.4,
          }}
        >
          {total} tasks &middot; {ideas} ideas &middot; {scored} scored &middot; {unscored} unscored
        </div>
      </div>

      {/* Status */}
      <div>
        <GrpLabel>Status</GrpLabel>
        <div className="flex flex-col gap-[1px]">
          {STATUS_OPTIONS.map((opt) => {
            const active = filters.status.includes(opt.value);
            const count = statusFacetTasks.filter((t) => t.status === opt.value).length;
            return (
              <FlRow
                key={opt.value}
                active={active}
                onClick={() =>
                  onFiltersChange({
                    ...filters,
                    status: toggleArrayFilter(filters.status, opt.value),
                  })
                }
                dotClass={STATUS_DOT_V2[opt.value]}
                label={opt.label}
                count={count}
              />
            );
          })}
        </div>
      </div>

      {/* Kind */}
      <div>
        <GrpLabel>Kind</GrpLabel>
        <div className="flex flex-wrap" style={{ gap: "4px" }}>
          {KIND_OPTIONS.map((opt) => {
            const active = filters.kind.includes(opt.value);
            const count = kindFacetTasks.filter((t) => t.kind === opt.value).length;
            return (
              <ChipV2
                key={opt.value}
                active={active}
                onClick={() =>
                  onFiltersChange({
                    ...filters,
                    kind: toggleArrayFilter(filters.kind, opt.value),
                  })
                }
                activeClass={KIND_CHIP_V2[opt.value]}
                label={opt.label}
                count={count}
              />
            );
          })}
        </div>
      </div>

      {/* Delegation */}
      <div>
        <GrpLabel>Delegation</GrpLabel>
        <div className="flex flex-wrap" style={{ gap: "4px" }}>
          {DELEGATION_OPTIONS.map((opt) => {
            const active = filters.delegation.includes(opt.value);
            return (
              <ChipV2
                key={opt.value}
                active={active}
                onClick={() =>
                  onFiltersChange({
                    ...filters,
                    delegation: toggleArrayFilter(filters.delegation, opt.value),
                  })
                }
                activeClass={DELEGATION_CHIP_V2[opt.value]}
                label={opt.label}
              />
            );
          })}
        </div>
      </div>

      {/* Priority */}
      <div>
        <GrpLabel>Priority</GrpLabel>
        <div className="flex flex-wrap" style={{ gap: "4px" }}>
          {PRIORITY_OPTIONS.map((opt) => {
            const active = filters.priority.includes(opt.value);
            return (
              <ChipV2
                key={opt.value}
                active={active}
                onClick={() =>
                  onFiltersChange({
                    ...filters,
                    priority: toggleArrayFilter(filters.priority, opt.value),
                  })
                }
                label={opt.label}
              />
            );
          })}
        </div>
      </div>

      {/* Project */}
      <div>
        <GrpLabel>Project</GrpLabel>
        <div
          className="flex flex-col gap-[1px] overflow-y-auto"
          style={{ maxHeight: 220 }}
        >
          {projects.map((p) => {
            const active = filters.project.includes(p);
            const count = projectFacetTasks.filter((t) => t.project === p).length;
            return (
              <FlRow
                key={p}
                active={active}
                onClick={() =>
                  onFiltersChange({
                    ...filters,
                    project: toggleArrayFilter(filters.project, p),
                  })
                }
                label={p}
                count={count}
              />
            );
          })}
        </div>
      </div>

      {/* Clear all — always visible per target (greyed when no filters) */}
      <button
        type="button"
        disabled={!hasAny}
        onClick={() =>
          onFiltersChange({ status: [], kind: [], project: [], priority: [], delegation: [] })
        }
        className={`text-left transition-colors bg-transparent border-0 p-0 ${
          hasAny
            ? "text-pir-text-muted hover:text-pir-text-secondary cursor-pointer"
            : "text-pir-text-muted/40 cursor-not-allowed"
        }`}
        style={{
          fontFamily: "var(--pir-font-mono)",
          fontWeight: 500,
          fontSize: "10px",
          lineHeight: 1,
          padding: "6px 0",
        }}
      >
        Clear all filters
      </button>
    </div>
  );
}
