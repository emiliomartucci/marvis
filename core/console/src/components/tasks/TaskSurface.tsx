"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  createComment,
  createTask,
  getPrograms,
  getPullRequest,
  listTasks,
  mergePullRequest,
  requestPRChanges,
  updateTask,
} from "@/lib/api";
import type {
  CommentResponse,
  ProgramInfo,
  ProjectInfo,
  PullRequest,
  TaskResponse,
} from "@/lib/types";
import { useT } from "@/lib/i18n";
import { Drawer } from "@/components/ui/Drawer";
import {
  TASK_LIFECYCLE_COLUMNS,
  TASK_LIFECYCLE_STATUSES,
  addDays,
  delegationGlyph,
  formatMonthTitle,
  formatShortDate,
  getTaskDueDate,
  iceValue,
  isoDate,
  matchesOwnerFilter,
  parseTaskDescription,
  projectColor,
  taskActionDefinitions,
  taskColumnTasks,
  taskPrLabel,
  type TaskActionDefinition,
  type TaskLifecycleStatus,
  type TaskOwnerFilter,
  type TaskViewMode,
} from "./taskModel";

type ProjectFilter =
  | { type: "all" }
  | { type: "project"; value: string }
  | { type: "program"; value: string }
  | { type: "scope"; value: string }
  | { type: "kind"; value: string };

type TaskDictionary = ReturnType<typeof useT>["t"]["taskSurface"];

const VIEW_STORAGE_KEY = "marvis:task-view";
const DEFAULT_LIMIT = 500;

function readStoredView(): TaskViewMode {
  if (typeof window === "undefined") return "kanban";
  const value = window.localStorage.getItem(VIEW_STORAGE_KEY);
  return value === "project" || value === "calendar" || value === "kanban" ? value : "kanban";
}

function projectList(programs: ProgramInfo[]): ProjectInfo[] {
  return programs.flatMap((program) => program.projects);
}

function projectMap(programs: ProgramInfo[]): Map<string, ProjectInfo> {
  return new Map(projectList(programs).map((project) => [project.slug, project]));
}

function taskActivityByProject(tasks: TaskResponse[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const task of tasks) counts.set(task.project, (counts.get(task.project) ?? 0) + 1);
  return counts;
}

function projectMatchesFilter(project: ProjectInfo | undefined, filter: ProjectFilter): boolean {
  if (filter.type === "all") return true;
  if (!project) return false;
  if (filter.type === "project") return project.slug === filter.value;
  if (filter.type === "program") return project.program === filter.value;
  if (filter.type === "scope") return project.scope === filter.value;
  if (filter.type === "kind") return project.type === filter.value;
  return true;
}

function filterLabel(filter: ProjectFilter, programs: ProgramInfo[], projects: Map<string, ProjectInfo>, t: TaskDictionary): string {
  if (filter.type === "all") return t.filters.all;
  if (filter.type === "project") return projects.get(filter.value)?.name ?? filter.value;
  if (filter.type === "program") return programs.find((program) => program.name === filter.value)?.name ?? filter.value;
  if (filter.type === "scope") return filter.value === "personal" ? t.filters.personal : t.filters.work;
  if (filter.type === "kind") return filter.value === "code" ? t.filters.code : t.filters.noCode;
  return t.filters.all;
}

function classForSegment(active: boolean): string {
  return active
    ? "border-pir-accent bg-pir-accent/10 text-pir-text-primary"
    : "border-pir bg-pir-surface-1 text-pir-text-tertiary hover:text-pir-text-primary";
}

function TaskSegmentedControl({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (value: string) => void;
  options: Array<{ value: string; label: string }>;
}) {
  return (
    <div className="flex rounded border border-pir bg-pir-surface-0 p-0.5">
      {options.map((option) => {
        const active = value === option.value;
        return (
          <button
            key={option.value}
            type="button"
            onClick={() => onChange(option.value)}
            className={`h-8 rounded px-3 text-label transition-colors ${active ? "bg-pir-surface-2 text-pir-text-primary" : "text-pir-text-tertiary hover:text-pir-text-primary"}`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function TaskCard({
  task,
  project,
  pr,
  onOpen,
  onMove,
  t,
  locale,
}: {
  task: TaskResponse;
  project: ProjectInfo | undefined;
  pr?: PullRequest | null;
  onOpen: (task: TaskResponse) => void;
  onMove?: (task: TaskResponse) => void;
  t: TaskDictionary;
  locale: string;
}) {
  const dueDate = getTaskDueDate(task);
  const prLabel = taskPrLabel(task, pr);
  const owner = delegationGlyph(task.delegation);

  return (
    <button
      type="button"
      data-tour="task-card"
      draggable={Boolean(onMove)}
      onDragStart={() => onMove?.(task)}
      onClick={() => onOpen(task)}
      className="w-full rounded border border-pir bg-pir-surface-1 px-3 py-2 text-left transition-colors hover:border-pir-accent focus:outline-none focus:ring-1 focus:ring-pir-accent active:cursor-grabbing"
      style={{ borderLeftColor: projectColor(project), borderLeftWidth: 3 }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate text-label font-medium text-pir-text-primary">{task.title}</span>
        <span className="shrink-0 font-mono text-[10px] text-pir-text-muted">{task.id.slice(-6)}</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-caption text-pir-text-muted">
        <span className="inline-flex items-center gap-1">
          <span aria-hidden>{owner === "person" ? "👤" : "◇"}</span>
          {owner === "person" ? t.card.human : t.card.agent}
        </span>
        <span className="inline-flex items-center gap-1" title={t.card.ease}>
          <span aria-hidden>⚡</span>
          <span className="font-mono">{iceValue(task.ease)}</span>
        </span>
        <span className="inline-flex items-center gap-1" title={t.card.impact}>
          <span aria-hidden>◎</span>
          <span className="font-mono">{iceValue(task.impact)}</span>
        </span>
        {prLabel && (
          <span className="inline-flex items-center gap-1 rounded bg-pir-accent/10 px-1.5 py-0.5 font-mono text-[10px] text-pir-accent">
            <span aria-hidden>⎇</span>
            {prLabel}
          </span>
        )}
        <span className="ml-auto inline-flex items-center gap-1 font-mono text-[10px]">
          {dueDate ? formatShortDate(dueDate, locale) : t.card.noDue}
        </span>
      </div>
    </button>
  );
}

function CollapsedColumn({
  label,
  count,
  onExpand,
}: {
  label: string;
  count: number;
  onExpand: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onExpand}
      className="flex h-full min-h-[260px] w-12 shrink-0 flex-col items-center rounded border border-pir bg-pir-surface-0 px-1 py-3 text-pir-text-tertiary transition-colors hover:border-pir-accent hover:text-pir-text-primary"
    >
      <span className="rounded bg-pir-surface-2 px-1.5 py-0.5 font-mono text-[10px]">{count}</span>
      <span className="mt-3 text-label font-medium [writing-mode:vertical-rl]">{label}</span>
    </button>
  );
}

function KanbanView({
  tasks,
  projects,
  prs,
  onOpen,
  onStatusMove,
  t,
  locale,
}: {
  tasks: TaskResponse[];
  projects: Map<string, ProjectInfo>;
  prs: Map<string, PullRequest | null>;
  onOpen: (task: TaskResponse) => void;
  onStatusMove: (task: TaskResponse, status: TaskLifecycleStatus) => void;
  t: TaskDictionary;
  locale: string;
}) {
  const [draggedTask, setDraggedTask] = useState<TaskResponse | null>(null);
  const [collapsed, setCollapsed] = useState<Record<TaskLifecycleStatus, boolean>>(() =>
    TASK_LIFECYCLE_COLUMNS.reduce((acc, column) => {
      acc[column.status] = column.collapsedByDefault;
      return acc;
    }, {} as Record<TaskLifecycleStatus, boolean>),
  );
  const columns = taskColumnTasks(tasks);

  function handleColumnDrop(status: TaskLifecycleStatus) {
    if (!draggedTask || draggedTask.status === status) {
      setDraggedTask(null);
      return;
    }
    onStatusMove(draggedTask, status);
    setDraggedTask(null);
  }

  return (
    <div className="flex min-h-0 flex-1 gap-3 overflow-x-auto px-5 py-4">
      {TASK_LIFECYCLE_COLUMNS.map((column) => {
        const items = columns[column.status] ?? [];
        const label = t.columns[column.status];
        if (collapsed[column.status]) {
          return (
            <CollapsedColumn
              key={column.status}
              label={label}
              count={items.length}
              onExpand={() => setCollapsed((current) => ({ ...current, [column.status]: false }))}
            />
          );
        }

        return (
          <section
            key={column.status}
            className="flex min-w-[238px] max-w-[320px] flex-1 flex-col rounded border border-pir bg-pir-surface-0 p-2"
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => handleColumnDrop(column.status)}
          >
            <div className="mb-2 flex items-center justify-between px-1">
              <div className="flex items-center gap-2">
                <h2 className="text-label font-semibold text-pir-text-primary">{label}</h2>
                <span className="rounded bg-pir-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-pir-text-muted">
                  {items.length}
                </span>
              </div>
              {column.collapsedByDefault && (
                <button
                  type="button"
                  onClick={() => setCollapsed((current) => ({ ...current, [column.status]: true }))}
                  className="rounded px-1.5 py-1 text-caption text-pir-text-muted hover:bg-pir-surface-1 hover:text-pir-text-primary"
                  aria-label={`Collapse ${label}`}
                >
                  x
                </button>
              )}
            </div>
            <div className="flex min-h-[180px] flex-col gap-2">
              {items.map((task) => (
                <TaskCard
                  key={task.id}
                  task={task}
                  project={projects.get(task.project)}
                  pr={prs.get(task.id)}
                  onOpen={onOpen}
                  onMove={setDraggedTask}
                  t={t}
                  locale={locale}
                />
              ))}
              {items.length === 0 && (
                <div className="rounded border border-dashed border-pir px-3 py-6 text-center text-caption text-pir-text-muted">
                  {t.empty}
                </div>
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function ProjectLaneView({
  tasks,
  projects,
  onOpen,
  t,
  locale,
}: {
  tasks: TaskResponse[];
  projects: Map<string, ProjectInfo>;
  onOpen: (task: TaskResponse) => void;
  t: TaskDictionary;
  locale: string;
}) {
  const today = useMemo(() => {
    const base = new Date();
    base.setHours(0, 0, 0, 0);
    return base;
  }, []);
  const days = useMemo(() => Array.from({ length: 14 }, (_, index) => addDays(today, index)), [today]);
  const todayIso = isoDate(today);
  const tasksByProject = useMemo(() => {
    const grouped = new Map<string, TaskResponse[]>();
    for (const task of tasks) {
      const list = grouped.get(task.project) ?? [];
      list.push(task);
      grouped.set(task.project, list);
    }
    return Array.from(grouped.entries()).sort(([left], [right]) => left.localeCompare(right));
  }, [tasks]);

  return (
    <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
      <div className="min-w-[900px] overflow-hidden rounded border border-pir bg-pir-surface-0">
        <div className="grid border-b border-pir bg-pir-surface-1" style={{ gridTemplateColumns: "220px repeat(14, minmax(44px,1fr))" }}>
          <div className="border-r border-pir px-3 py-2 font-mono text-caption uppercase text-pir-text-muted">
            {t.filters.projectResults}
          </div>
          {days.map((day) => {
            const dayIso = isoDate(day);
            return (
              <div
                key={dayIso}
                className={`border-r border-pir px-1 py-2 text-center last:border-r-0 ${dayIso === todayIso ? "bg-pir-accent/10 text-pir-accent" : "text-pir-text-tertiary"}`}
              >
                <div className="font-mono text-[10px]">{formatShortDate(dayIso, locale)}</div>
              </div>
            );
          })}
        </div>
        {tasksByProject.map(([projectSlug, projectTasks]) => {
          const project = projects.get(projectSlug);
          const rowsByDate = new Map<string, number>();
          return (
            <div
              key={projectSlug}
              className="grid min-h-16 border-b border-pir last:border-b-0"
              style={{ gridTemplateColumns: "220px repeat(14, minmax(44px,1fr))" }}
            >
              <div className="flex items-center gap-2 border-r border-pir px-3 py-3">
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: projectColor(project) }} />
                <span className="min-w-0 truncate text-label font-medium text-pir-text-primary">
                  {project?.name ?? projectSlug}
                </span>
              </div>
              {days.map((day) => {
                const dayIso = isoDate(day);
                const dueTasks = projectTasks.filter((task) => getTaskDueDate(task) === dayIso);
                return (
                  <div
                    key={dayIso}
                    className={`min-h-16 border-r border-pir p-1 last:border-r-0 ${dayIso === todayIso ? "bg-pir-accent/5" : ""}`}
                  >
                    <div className="flex flex-col gap-1">
                      {dueTasks.map((task) => {
                        const row = rowsByDate.get(dayIso) ?? 0;
                        rowsByDate.set(dayIso, row + 1);
                        return (
                          <LaneTaskMarker
                            key={task.id}
                            task={task}
                            project={project}
                            row={row}
                            onOpen={onOpen}
                          />
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LaneTaskMarker({
  task,
  project,
  row,
  onOpen,
}: {
  task: TaskResponse;
  project: ProjectInfo | undefined;
  row: number;
  onOpen: (task: TaskResponse) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onOpen(task)}
      className="truncate rounded border border-pir bg-pir-surface-1 px-1.5 py-1 text-left text-[10px] text-pir-text-secondary hover:border-pir-accent"
      style={{ marginTop: row > 0 ? 2 : 0, borderLeftColor: projectColor(project), borderLeftWidth: 2 }}
      title={task.title}
    >
      <span aria-hidden>◆ </span>
      {task.title}
    </button>
  );
}

function CalendarView({
  tasks,
  projects,
  onOpen,
  t,
  locale,
}: {
  tasks: TaskResponse[];
  projects: Map<string, ProjectInfo>;
  onOpen: (task: TaskResponse) => void;
  t: TaskDictionary;
  locale: string;
}) {
  const [monthOffset, setMonthOffset] = useState(0);
  const base = useMemo(() => {
    const date = new Date();
    date.setDate(1);
    date.setMonth(date.getMonth() + monthOffset);
    date.setHours(0, 0, 0, 0);
    return date;
  }, [monthOffset]);
  const todayIso = isoDate(new Date());
  const cells = useMemo(() => {
    const start = new Date(base);
    const day = (start.getDay() + 6) % 7;
    start.setDate(1 - day);
    return Array.from({ length: 42 }, (_, index) => addDays(start, index));
  }, [base]);

  return (
    <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
      <div className="mb-3 flex max-w-[980px] items-center justify-between">
        <h2 className="text-heading capitalize text-pir-text-primary">{formatMonthTitle(base, locale)}</h2>
        <div className="flex gap-1">
          <button type="button" className="h-8 w-8 rounded border border-pir text-pir-text-tertiary hover:text-pir-text-primary" onClick={() => setMonthOffset((value) => value - 1)}>
            {"<"}
          </button>
          {monthOffset !== 0 && (
            <button type="button" className="h-8 rounded border border-pir px-2 text-caption text-pir-text-tertiary hover:text-pir-text-primary" onClick={() => setMonthOffset(0)}>
              {t.drawer.dueTitle}
            </button>
          )}
          <button type="button" className="h-8 w-8 rounded border border-pir text-pir-text-tertiary hover:text-pir-text-primary" onClick={() => setMonthOffset((value) => value + 1)}>
            {">"}
          </button>
        </div>
      </div>
      <div className="grid max-w-[980px] grid-cols-7 overflow-hidden rounded border border-pir bg-pir-surface-0">
        {["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"].map((label) => (
          <div key={label} className="border-b border-r border-pir bg-pir-surface-1 px-2 py-2 font-mono text-[10px] text-pir-text-muted last:border-r-0">
            {label}
          </div>
        ))}
        {cells.map((cell) => {
          const cellIso = isoDate(cell);
          const inMonth = cell.getMonth() === base.getMonth();
          const dueTasks = tasks.filter((task) => getTaskDueDate(task) === cellIso);
          return (
            <div
              key={cellIso}
              className={`min-h-[112px] border-r border-t border-pir p-1.5 ${cellIso === todayIso ? "bg-pir-accent/5" : ""} ${inMonth ? "" : "opacity-40"}`}
            >
              <div className={`mb-1 font-mono text-[11px] ${cellIso === todayIso ? "text-pir-accent" : "text-pir-text-muted"}`}>
                {cell.getDate()}
              </div>
              <div className="flex flex-col gap-1">
                {dueTasks.slice(0, 3).map((task) => {
                  const project = projects.get(task.project);
                  return (
                    <button
                      key={task.id}
                      type="button"
                      onClick={() => onOpen(task)}
                      className="truncate rounded bg-pir-surface-1 px-1.5 py-1 text-left text-[10px] text-pir-text-secondary hover:text-pir-text-primary"
                      style={{ borderLeft: `2px solid ${projectColor(project)}` }}
                    >
                      <span aria-hidden>◆ </span>
                      {task.title}
                    </button>
                  );
                })}
                {dueTasks.length > 3 && (
                  <button
                    type="button"
                    onClick={() => onOpen(dueTasks[3])}
                    className="text-left font-mono text-[10px] text-pir-text-muted"
                  >
                    +{dueTasks.length - 3}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function TaskActionBar({
  task,
  onAction,
  t,
}: {
  task: TaskResponse;
  onAction: (action: TaskActionDefinition, feedback?: string) => void;
  t: TaskDictionary;
}) {
  const [feedbackAction, setFeedbackAction] = useState<TaskActionDefinition | null>(null);
  const [feedback, setFeedback] = useState("");
  const actions = taskActionDefinitions(task);

  function handleAction(action: TaskActionDefinition) {
    if (action.needsFeedback) {
      setFeedbackAction(action);
      return;
    }
    onAction(action);
  }

  return (
    <div className="flex flex-col gap-2">
      {feedbackAction && (
        <div className="rounded border border-pir bg-pir-base p-2">
          <textarea
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder={t.drawer.feedbackPlaceholder}
            className="min-h-20 w-full resize-none rounded border border-pir bg-pir-surface-0 px-2 py-2 text-body text-pir-text-primary outline-none focus:border-pir-accent"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              className="h-8 rounded border border-pir px-3 text-caption text-pir-text-tertiary hover:text-pir-text-primary"
              onClick={() => {
                setFeedbackAction(null);
                setFeedback("");
              }}
            >
              {t.drawer.cancel}
            </button>
            <button
              type="button"
              disabled={!feedback.trim()}
              className="h-8 rounded bg-pir-accent px-3 text-caption font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => {
                onAction(feedbackAction, feedback.trim());
                setFeedbackAction(null);
                setFeedback("");
              }}
            >
              {t.actions.return}
            </button>
          </div>
        </div>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        {actions.map((action) => (
          <button
            key={action.id}
            type="button"
            onClick={() => handleAction(action)}
            className={`h-9 rounded px-3 text-label font-semibold transition-colors ${
              action.id === "start" || action.id === "complete" || action.id === "approve" || action.id === "send_review"
                ? "bg-pir-accent text-pir-base hover:bg-pir-accent/90"
                : "border border-pir text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary"
            }`}
          >
            {t.actions[action.id]}
          </button>
        ))}
      </div>
    </div>
  );
}

export function TaskDetailDrawer({
  task,
  open,
  project,
  pr,
  comments,
  onClose,
  onPostpone,
  onAddNote,
  onAction,
  t,
  locale,
}: {
  task: TaskResponse | null;
  open: boolean;
  project: ProjectInfo | undefined;
  pr: PullRequest | null;
  comments: CommentResponse[];
  onClose: () => void;
  onPostpone: (task: TaskResponse, dueDate: string) => void;
  onAddNote: (task: TaskResponse, note: string) => void;
  onAction: (task: TaskResponse, action: TaskActionDefinition, feedback?: string) => void;
  t: TaskDictionary;
  locale: string;
}) {
  const [postponing, setPostponing] = useState(false);
  const [newDueDate, setNewDueDate] = useState("");
  const [note, setNote] = useState("");
  const parsed = parseTaskDescription(task?.description);

  if (!task) {
    return (
      <Drawer open={open} onClose={onClose} header={<span />}>
        <span />
      </Drawer>
    );
  }

  const dueDate = getTaskDueDate(task);
  const blockedBy = task.blocked_by;
  const timeline = [
    { label: t.drawer.created, value: task.created_at },
    { label: t.drawer.updated, value: task.updated_at },
    ...comments.map((comment) => ({ label: comment.created_by, value: comment.created_at })),
  ];
  let delegationScore: number | null = null;
  if (task.delegation === "human") delegationScore = 10;
  else if (task.delegation) delegationScore = 5;

  const header = (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="font-mono text-caption text-pir-text-muted">{task.id}</p>
        <h2 id="task-drawer-title" className="mt-1 truncate text-heading text-pir-text-primary">
          {task.title}
        </h2>
      </div>
      <button
        type="button"
        onClick={onClose}
        className="h-8 w-8 rounded border border-pir text-pir-text-muted hover:text-pir-text-primary"
        aria-label="Close task"
      >
        x
      </button>
    </div>
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      titleId="task-drawer-title"
      dataTour="task-drawer"
      header={header}
      actions={<TaskActionBar task={task} onAction={(action, feedback) => onAction(task, action, feedback)} t={t} />}
    >
      <div className="space-y-5">
        <section>
          <p className="font-mono text-caption uppercase text-pir-text-muted">{project?.name ?? task.project}</p>
          <h3 className="mt-3 font-mono text-caption uppercase text-pir-text-muted">{t.drawer.descriptionTitle}</h3>
          {parsed.kind === "structured" ? (
            <div className="mt-2 space-y-2">
              <p className="text-body leading-6 text-pir-text-secondary">
                <span className="text-pir-text-primary">{parsed.do}</span>
                <span className="text-pir-text-muted"> perche </span>
                {parsed.why}.
              </p>
              <div className="rounded border border-pir-warning/40 bg-pir-warning/10 px-3 py-2 text-body text-pir-text-secondary">
                <span className="font-semibold text-pir-warning">{t.drawer.warningPrefix} </span>
                {parsed.watch}
              </div>
            </div>
          ) : (
            <div className="mt-2 rounded border border-pir bg-pir-base px-3 py-2 text-body text-pir-text-secondary">
              {parsed.text || t.drawer.noDescription}
            </div>
          )}
        </section>

        <section className="rounded border border-pir bg-pir-base p-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.dueTitle}</p>
              <p className="mt-1 font-mono text-label text-pir-text-primary">
                {dueDate ? formatShortDate(dueDate, locale) : t.card.noDue}
              </p>
            </div>
            {!postponing && (
              <button
                type="button"
                className="h-8 rounded border border-pir px-3 text-caption text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary"
                onClick={() => {
                  setPostponing(true);
                  setNewDueDate("");
                }}
              >
                {t.drawer.postpone}
              </button>
            )}
          </div>
          {postponing && (
            <div className="mt-3 flex gap-2 border-t border-pir pt-3">
              <input
                aria-label={t.drawer.newDate}
                type="date"
                value={newDueDate}
                onChange={(event) => setNewDueDate(event.target.value)}
                className="h-9 min-w-0 flex-1 rounded border border-pir bg-pir-surface-0 px-2 font-mono text-caption text-pir-text-primary outline-none focus:border-pir-accent"
              />
              <button
                type="button"
                disabled={!newDueDate}
                className="h-9 rounded bg-pir-accent px-3 text-caption font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-50"
                onClick={() => {
                  if (!newDueDate) return;
                  onPostpone(task, newDueDate);
                  setPostponing(false);
                  setNewDueDate("");
                }}
              >
                {t.drawer.confirm}
              </button>
            </div>
          )}
        </section>

        <section>
          <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.iceTitle}</h3>
          <div className="mt-2 grid grid-cols-4 gap-2">
            {[
              ["I", task.impact],
              ["C", task.confidence],
              ["E", task.ease],
              ["D", delegationScore],
            ].map(([label, value]) => (
              <div key={label as string} className="rounded border border-pir bg-pir-base p-2 text-center">
                <p className="font-mono text-caption text-pir-text-muted">{label}</p>
                <p className="mt-1 text-heading text-pir-text-primary">{iceValue(value as number | null)}</p>
              </div>
            ))}
          </div>
        </section>

        {task.pr_status && (
          <section className="rounded border border-pir bg-pir-base p-3">
            <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.prTitle}</h3>
            <div className="mt-2 space-y-2 text-body text-pir-text-secondary">
              <div className="flex justify-between gap-3">
                <span className="text-pir-text-muted">status</span>
                <span className="font-mono">{pr?.status ?? task.pr_status}</span>
              </div>
              <div className="flex justify-between gap-3">
                <span className="text-pir-text-muted">branch</span>
                <span className="truncate font-mono">{pr?.branch ?? "-"}</span>
              </div>
              {pr?.diff && (
                <div className="flex gap-3 font-mono text-caption">
                  <span className="text-pir-success">+{pr.diff.stats.additions}</span>
                  <span className="text-pir-error">-{pr.diff.stats.deletions}</span>
                  <span className="text-pir-text-muted">{pr.diff.stats.files_changed} files</span>
                </div>
              )}
              <Link
                href={`/graph/pr-impact?prId=${encodeURIComponent(pr?.id ?? task.id)}`}
                className="inline-flex h-8 items-center rounded border border-pir px-3 text-caption text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary"
              >
                {t.drawer.diffLink}
              </Link>
            </div>
          </section>
        )}

        {blockedBy && (
          <section>
            <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.dependencies}</h3>
            <div className="mt-2 rounded border border-pir-warning/40 bg-pir-warning/10 px-3 py-2 text-body text-pir-text-secondary">
              {t.drawer.blockedBy} <span className="font-mono">{blockedBy}</span>
            </div>
          </section>
        )}

        <section>
          <div className="flex items-center justify-between">
            <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.notes}</h3>
            <span className="text-caption text-pir-text-muted">{t.drawer.notesHelper}</span>
          </div>
          <div className="mt-2 space-y-2">
            {comments.map((comment) => (
              <div key={comment.id} className="rounded border border-pir bg-pir-base px-3 py-2">
                <p className="text-body text-pir-text-secondary">{comment.body}</p>
                <p className="mt-1 font-mono text-[10px] text-pir-text-muted">
                  {comment.created_by} · {comment.created_at.slice(0, 16).replace("T", " ")}
                </p>
              </div>
            ))}
          </div>
          <div className="mt-2 flex gap-2 rounded border border-pir bg-pir-base p-2">
            <input
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder={t.drawer.notePlaceholder}
              className="h-8 min-w-0 flex-1 bg-transparent text-body text-pir-text-primary outline-none"
            />
            <button
              type="button"
              disabled={!note.trim()}
              className="h-8 rounded bg-pir-accent px-3 text-caption font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => {
                onAddNote(task, note.trim());
                setNote("");
              }}
            >
              {t.drawer.addNote}
            </button>
          </div>
        </section>

        <section>
          <h3 className="font-mono text-caption uppercase text-pir-text-muted">{t.drawer.timeline}</h3>
          <div className="mt-3 space-y-0 pl-1">
            {timeline.map((item, index) => (
              <div key={`${item.label}-${item.value}-${index}`} className="relative flex gap-3 pb-4 last:pb-0">
                {index < timeline.length - 1 && <span className="absolute left-[5px] top-3 bottom-0 w-px bg-pir-border" aria-hidden />}
                <span className="relative mt-1 h-3 w-3 shrink-0 rounded-full border border-pir-accent bg-pir-surface-0" aria-hidden />
                <div>
                  <p className="text-caption font-semibold text-pir-text-secondary">{item.label}</p>
                  <p className="font-mono text-[10px] text-pir-text-muted">{item.value.slice(0, 16).replace("T", " ")}</p>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </Drawer>
  );
}

function FollowUpModal({
  task,
  onClose,
  onCreate,
  t,
}: {
  task: TaskResponse;
  onClose: () => void;
  onCreate: (title: string) => void;
  t: TaskDictionary;
}) {
  const [title, setTitle] = useState(`${t.followUp.prefix} ${task.title}`);

  return (
    <div className="fixed inset-0 z-[95] flex items-center justify-center p-4">
      <button type="button" className="absolute inset-0 cursor-default bg-pir-base/70" aria-label={t.followUp.skip} onClick={onClose} />
      <section className="relative w-[min(92vw,420px)] rounded border border-pir bg-pir-surface-0 p-4 text-pir-text-primary shadow-xl">
        <h2 className="text-heading">{t.followUp.title}</h2>
        <p className="mt-1 text-body text-pir-text-secondary">{t.followUp.body}</p>
        <label className="mt-4 block">
          <span className="font-mono text-caption uppercase text-pir-text-muted">{t.followUp.field}</span>
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            className="mt-2 h-9 w-full rounded border border-pir bg-pir-base px-2 text-body text-pir-text-primary outline-none focus:border-pir-accent"
          />
        </label>
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" className="h-9 rounded border border-pir px-3 text-label text-pir-text-secondary hover:text-pir-text-primary" onClick={onClose}>
            {t.followUp.skip}
          </button>
          <button
            type="button"
            disabled={!title.trim()}
            className="h-9 rounded bg-pir-accent px-3 text-label font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => onCreate(title.trim())}
          >
            {t.followUp.create}
          </button>
        </div>
      </section>
    </div>
  );
}

function FilterPanel({
  programs,
  projects,
  onPick,
  t,
}: {
  programs: ProgramInfo[];
  projects: ProjectInfo[];
  onPick: (filter: ProjectFilter) => void;
  t: TaskDictionary;
}) {
  const [query, setQuery] = useState("");
  const filteredProjects = projects
    .filter((project) => project.name.toLowerCase().includes(query.toLowerCase()))
    .slice(0, 40);

  return (
    <div className="rounded border border-pir bg-pir-surface-0 p-3">
      <label className="block">
        <span className="font-mono text-caption uppercase text-pir-text-muted">{t.filters.search}</span>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t.filters.searchPlaceholder}
          className="mt-2 h-9 w-full rounded border border-pir bg-pir-base px-2 text-body text-pir-text-primary outline-none focus:border-pir-accent"
        />
      </label>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <div>
          <p className="mb-2 font-mono text-caption uppercase text-pir-text-muted">{t.filters.program}</p>
          <div className="flex flex-wrap gap-1">
            {programs.slice(0, 10).map((program) => (
              <button key={program.name} type="button" className="rounded border border-pir px-2 py-1 text-caption text-pir-text-secondary hover:border-pir-accent" onClick={() => onPick({ type: "program", value: program.name })}>
                {program.name}
              </button>
            ))}
          </div>
        </div>
        <div>
          <p className="mb-2 font-mono text-caption uppercase text-pir-text-muted">{t.filters.scope}</p>
          <div className="flex flex-wrap gap-1">
            <button type="button" className="rounded border border-pir px-2 py-1 text-caption text-pir-text-secondary hover:border-pir-accent" onClick={() => onPick({ type: "scope", value: "work" })}>
              {t.filters.work}
            </button>
            <button type="button" className="rounded border border-pir px-2 py-1 text-caption text-pir-text-secondary hover:border-pir-accent" onClick={() => onPick({ type: "scope", value: "personal" })}>
              {t.filters.personal}
            </button>
          </div>
        </div>
        <div>
          <p className="mb-2 font-mono text-caption uppercase text-pir-text-muted">{t.filters.type}</p>
          <div className="flex flex-wrap gap-1">
            <button type="button" className="rounded border border-pir px-2 py-1 text-caption text-pir-text-secondary hover:border-pir-accent" onClick={() => onPick({ type: "kind", value: "code" })}>
              {t.filters.code}
            </button>
            <button type="button" className="rounded border border-pir px-2 py-1 text-caption text-pir-text-secondary hover:border-pir-accent" onClick={() => onPick({ type: "kind", value: "work" })}>
              {t.filters.noCode}
            </button>
          </div>
        </div>
      </div>
      <div className="mt-3 max-h-48 overflow-y-auto border-t border-pir pt-2">
        <p className="mb-2 font-mono text-caption uppercase text-pir-text-muted">
          {filteredProjects.length} {t.filters.projectResults}
        </p>
        <div className="grid gap-1 md:grid-cols-2">
          {filteredProjects.map((project) => (
            <button
              key={project.slug}
              type="button"
              onClick={() => onPick({ type: "project", value: project.slug })}
              className="flex items-center gap-2 rounded px-2 py-1.5 text-left text-caption text-pir-text-secondary hover:bg-pir-surface-1 hover:text-pir-text-primary"
            >
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: projectColor(project) }} />
              <span className="min-w-0 flex-1 truncate">{project.name}</span>
              <span className="font-mono text-[10px] text-pir-text-muted">{project.program ?? "-"}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function TaskSurfaceContent() {
  const { t, locale } = useT();
  const searchParams = useSearchParams();
  const tt = t.taskSurface;
  const [view, setView] = useState<TaskViewMode>(() => readStoredView());
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [programs, setPrograms] = useState<ProgramInfo[]>([]);
  const [projectFilter, setProjectFilter] = useState<ProjectFilter>({ type: "all" });
  const [ownerFilter, setOwnerFilter] = useState<TaskOwnerFilter>("all");
  const [query, setQuery] = useState("");
  const [filterPanelOpen, setFilterPanelOpen] = useState(false);
  const [selectedTask, setSelectedTask] = useState<TaskResponse | null>(null);
  const [prByTask, setPrByTask] = useState<Map<string, PullRequest | null>>(new Map());
  const [followUpTask, setFollowUpTask] = useState<TaskResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const projects = useMemo(() => projectMap(programs), [programs]);
  const flatProjects = useMemo(() => projectList(programs), [programs]);
  const activeProjectLabel = filterLabel(projectFilter, programs, projects, tt);
  const comments = selectedTask?.comments ?? [];
  const queryProject = searchParams.get("project");

  useEffect(() => {
    if (!queryProject) return;
    setProjectFilter({ type: "project", value: queryProject });
  }, [queryProject]);

  useEffect(() => {
    window.localStorage.setItem(VIEW_STORAGE_KEY, view);
  }, [view]);

  useEffect(() => {
    const controller = new AbortController();
    let delegationParam: string | undefined;
    if (ownerFilter === "human") delegationParam = "human";

    setLoading(true);
    setError(null);
    Promise.all([
      listTasks(
        {
          detailed: true,
          limit: DEFAULT_LIMIT,
          sort: "updated_at:desc",
          project: projectFilter.type === "project" ? projectFilter.value : undefined,
          status: TASK_LIFECYCLE_STATUSES.join(","),
          delegation: delegationParam,
        },
        { signal: controller.signal },
      ),
      getPrograms({ signal: controller.signal }),
    ])
      .then(([nextTasks, nextPrograms]) => {
        setTasks(nextTasks);
        setPrograms(nextPrograms);
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : tt.error);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [ownerFilter, projectFilter, tt.error]);

  useEffect(() => {
    if (!selectedTask?.pr_status || prByTask.has(selectedTask.id)) return;
    const controller = new AbortController();
    getPullRequest(selectedTask.id, { signal: controller.signal })
      .then((pr) => setPrByTask((current) => new Map(current).set(selectedTask.id, pr)))
      .catch(() => setPrByTask((current) => new Map(current).set(selectedTask.id, null)));
    return () => controller.abort();
  }, [prByTask, selectedTask]);

  const filteredTasks = useMemo(() => {
    return tasks.filter((task) => {
      const project = projects.get(task.project);
      const queryMatch = !query.trim() ||
        task.title.toLowerCase().includes(query.toLowerCase()) ||
        (task.description ?? "").toLowerCase().includes(query.toLowerCase());
      return (
        TASK_LIFECYCLE_STATUSES.includes(task.status as TaskLifecycleStatus) &&
        projectMatchesFilter(project, projectFilter) &&
        matchesOwnerFilter(task, ownerFilter) &&
        queryMatch
      );
    });
  }, [ownerFilter, projectFilter, projects, query, tasks]);

  const topProjects = useMemo(() => {
    const activity = taskActivityByProject(tasks);
    return flatProjects
      .slice()
      .sort((left, right) => {
        const activityDelta = (activity.get(right.slug) ?? 0) - (activity.get(left.slug) ?? 0);
        if (activityDelta !== 0) return activityDelta;
        return (right.last_status_update ?? right.last_handoff ?? "").localeCompare(left.last_status_update ?? left.last_handoff ?? "");
      })
      .slice(0, 5);
  }, [flatProjects, tasks]);

  function patchTaskInState(updated: TaskResponse) {
    setTasks((current) => current.map((task) => (task.id === updated.id ? { ...task, ...updated } : task)));
    setSelectedTask((current) => (current?.id === updated.id ? { ...current, ...updated } : current));
  }

  async function handlePostpone(task: TaskResponse, dueDate: string) {
    const updated = await updateTask(task.id, { due_date: dueDate });
    patchTaskInState(updated);
  }

  async function handleAddNote(task: TaskResponse, note: string) {
    if (!note) return;
    const created = await createComment({
      target_type: "task",
      target_id: task.id,
      body: note,
      status: "info",
      parent_id: null,
    });
    const nextTask = {
      ...task,
      comments: [...(task.comments ?? []), created],
    };
    patchTaskInState(nextTask);
  }

  async function handleAction(task: TaskResponse, action: TaskActionDefinition, feedback?: string) {
    if (action.id === "return" && feedback && task.pr_status) {
      await requestPRChanges(task.id, feedback);
      const updated = { ...task, status: "in_progress" as const, review_feedback: feedback };
      patchTaskInState(updated);
      setSelectedTask(null);
      return;
    } else if (action.id === "return" && feedback) {
      await createComment({
        target_type: "task",
        target_id: task.id,
        body: feedback,
        status: "question",
        parent_id: null,
      });
    }
    if (action.id === "approve" && task.pr_status) {
      await mergePullRequest(task.id);
      const updated = { ...task, status: "completed" as const, pr_status: "merged" as const };
      patchTaskInState(updated);
      setSelectedTask(null);
      if (action.opensFollowUp) setFollowUpTask(updated);
      return;
    }
    const updated = await updateTask(task.id, { status: action.nextStatus });
    patchTaskInState(updated);
    setSelectedTask(null);
    if (action.opensFollowUp) {
      setFollowUpTask(updated);
    }
  }

  async function handleStatusMove(task: TaskResponse, status: TaskLifecycleStatus) {
    try {
      const updated = await updateTask(task.id, { status });
      patchTaskInState(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : tt.error);
    }
  }

  async function handleCreateFollowUp(title: string) {
    if (!followUpTask) return;
    const created = await createTask({
      title,
      description: `Follow-up linked to ${followUpTask.id}: ${followUpTask.title}`,
      project: followUpTask.project,
      source: "console",
      priority: "medium",
      kind: "normal",
      tags: ["follow-up"],
      completion_mode: followUpTask.completion_mode,
    });
    setTasks((current) => [created, ...current]);
    setFollowUpTask(null);
  }

  function body(): ReactNode {
    if (loading) {
      return <div className="p-6 text-body text-pir-text-muted">{tt.loading}</div>;
    }
    if (error) {
      return (
        <div className="p-6">
          <div className="rounded border border-pir-error/40 bg-pir-error/10 p-4 text-body text-pir-text-secondary">
            {tt.error} <span className="font-mono">{error}</span>
          </div>
        </div>
      );
    }
    if (filteredTasks.length === 0) {
      return <div className="p-6 text-body text-pir-text-muted">{tt.empty}</div>;
    }
    if (view === "project") {
      return <ProjectLaneView tasks={filteredTasks} projects={projects} onOpen={setSelectedTask} t={tt} locale={locale} />;
    }
    if (view === "calendar") {
      return <CalendarView tasks={filteredTasks} projects={projects} onOpen={setSelectedTask} t={tt} locale={locale} />;
    }
    return (
      <KanbanView
        tasks={filteredTasks}
        projects={projects}
        prs={prByTask}
        onOpen={setSelectedTask}
        onStatusMove={handleStatusMove}
        t={tt}
        locale={locale}
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-pir-base text-pir-text-primary">
      <header className="shrink-0 border-b border-pir bg-pir-surface-0 px-5 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-heading text-pir-text-primary">{tt.title}</h1>
            <p className="mt-1 max-w-3xl text-body text-pir-text-secondary">{tt.subtitle}</p>
          </div>
          <div data-tour="task-views">
            <TaskSegmentedControl
              value={view}
              onChange={(next) => setView(next as TaskViewMode)}
              options={[
                { value: "kanban", label: tt.views.kanban },
                { value: "project", label: tt.views.project },
                { value: "calendar", label: tt.views.calendar },
              ]}
            />
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className={`h-8 rounded border px-3 text-caption transition-colors ${classForSegment(projectFilter.type === "all")}`}
            onClick={() => setProjectFilter({ type: "all" })}
          >
            {tt.filters.all}
          </button>
          {topProjects.map((project) => {
            const active = projectFilter.type === "project" && projectFilter.value === project.slug;
            return (
              <button
                key={project.slug}
                type="button"
                className={`flex h-8 items-center gap-2 rounded border px-3 text-caption transition-colors ${classForSegment(active)}`}
                onClick={() => setProjectFilter({ type: "project", value: project.slug })}
              >
                <span className="h-2 w-2 rounded-full" style={{ background: projectColor(project) }} />
                {project.name}
              </button>
            );
          })}
          {projectFilter.type !== "all" && !topProjects.some((project) => projectFilter.type === "project" && project.slug === projectFilter.value) && (
            <button
              type="button"
              className="h-8 rounded border border-pir-accent bg-pir-accent/10 px-3 text-caption text-pir-text-primary"
              onClick={() => setProjectFilter({ type: "all" })}
              aria-label={tt.filters.remove}
            >
              {activeProjectLabel} x
            </button>
          )}
          <button
            type="button"
            className={`h-8 rounded border px-3 text-caption transition-colors ${classForSegment(filterPanelOpen)}`}
            onClick={() => setFilterPanelOpen((open) => !open)}
          >
            {tt.filters.searchPanel}
          </button>
          <TaskSegmentedControl
            value={ownerFilter}
            onChange={(next) => setOwnerFilter(next as TaskOwnerFilter)}
            options={[
              { value: "all", label: tt.filters.ownerAll },
              { value: "agent", label: tt.filters.ownerAgent },
              { value: "human", label: tt.filters.ownerHuman },
            ]}
          />
          <div className="ml-auto flex h-8 items-center rounded border border-pir bg-pir-base px-2 font-mono text-caption text-pir-text-muted">
            {filteredTasks.length} task
          </div>
        </div>
        {filterPanelOpen && (
          <div className="mt-3">
            <FilterPanel
              programs={programs}
              projects={flatProjects}
              onPick={(filter) => {
                setProjectFilter(filter);
                setFilterPanelOpen(false);
              }}
              t={tt}
            />
          </div>
        )}
        <div className="mt-3">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={tt.filters.searchPlaceholder}
            className="h-9 w-full max-w-xl rounded border border-pir bg-pir-base px-3 text-body text-pir-text-primary outline-none placeholder:text-pir-text-muted focus:border-pir-accent"
          />
        </div>
      </header>

      {body()}

      <TaskDetailDrawer
        key={selectedTask?.id ?? "no-task"}
        task={selectedTask}
        open={selectedTask !== null}
        project={selectedTask ? projects.get(selectedTask.project) : undefined}
        pr={selectedTask ? prByTask.get(selectedTask.id) ?? null : null}
        comments={comments}
        onClose={() => setSelectedTask(null)}
        onPostpone={handlePostpone}
        onAddNote={handleAddNote}
        onAction={handleAction}
        t={tt}
        locale={locale}
      />

      {followUpTask && (
        <FollowUpModal
          task={followUpTask}
          onClose={() => setFollowUpTask(null)}
          onCreate={handleCreateFollowUp}
          t={tt}
        />
      )}
    </div>
  );
}

export default function TaskSurface() {
  return (
    <Suspense fallback={null}>
      <TaskSurfaceContent />
    </Suspense>
  );
}
