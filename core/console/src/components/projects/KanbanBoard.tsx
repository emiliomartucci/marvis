"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DndContext,
  DragOverlay,
  closestCorners,
  type DragEndEvent,
  type DragStartEvent,
  useDroppable,
} from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { API_BASE_URL } from "@/lib/config";
import TaskDetailModal from "./TaskDetailModal";
import type { TaskResponse, PrStatus } from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

type Task = TaskResponse;

const COLUMNS = ["pending", "approved", "in_progress", "completed"] as const;

const PR_BADGE: Record<PrStatus, { label: string; cls: string }> = {
  draft: { label: "Branch", cls: "bg-pir-text-muted/15 text-pir-text-muted" },
  open: { label: "Review", cls: "bg-pir-warning/15 text-pir-warning" },
  merging: { label: "Merging", cls: "bg-pir-accent/15 text-pir-accent" },
  merged: { label: "Merged", cls: "bg-pir-success/15 text-pir-success" },
  closed: { label: "Closed", cls: "bg-pir-error/15 text-pir-error" },
};
type Column = typeof COLUMNS[number];

const COLUMN_LABELS: Record<string, string> = {
  pending: "Pending",
  approved: "Approved",
  in_progress: "In Progress",
  completed: "Completed",
};

const COLUMN_COLORS: Record<string, string> = {
  pending: "bg-pir-warning",
  approved: "bg-pir-accent",
  in_progress: "bg-pir-success",
  completed: "bg-pir-text-muted",
};

const ALL_STATUSES = ["pending", "approved", "in_progress", "completed", "failed"] as const;

const PRIORITY_COLORS: Record<string, string> = {
  high: "border-l-pir-error",
  medium: "border-l-pir-warning",
  low: "border-l-pir-text-muted",
};

const PRIORITY_BADGES: Record<string, string> = {
  high: "P1",
  medium: "P2",
  low: "P3",
};

const PRIORITY_BADGE_COLORS: Record<string, string> = {
  high: "bg-pir-error/10 text-pir-error",
  medium: "bg-pir-warning/10 text-pir-warning",
  low: "bg-pir-text-muted/10 text-pir-text-muted",
};

const TAG_COLORS: Record<string, string> = {
  bug: "bg-pir-error/10 text-pir-error",
  feat: "bg-pir-accent/10 text-pir-accent",
  deploy: "bg-pir-success/10 text-pir-success",
};

function getTagColor(tag: string): string {
  return TAG_COLORS[tag] || "bg-pir-surface-0 text-pir-text-muted";
}

// --- Droppable Column ---

function DroppableColumn({ id, children }: { id: string; children: React.ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({ id });
  return (
    <div
      ref={setNodeRef}
      className={`flex-1 md:min-w-[200px] bg-pir-surface-0 rounded-lg p-2 transition-colors ${isOver ? "ring-1 ring-pir-accent" : ""}`}
    >
      <h3 className="text-caption font-medium text-pir-text-muted mb-2 px-1 flex items-center gap-1.5">
        <span className={`w-2 h-2 rounded-full ${COLUMN_COLORS[id] || "bg-pir-text-muted"}`} />
        {COLUMN_LABELS[id] || id}
      </h3>
      <div className="space-y-1 min-h-[100px]">{children}</div>
    </div>
  );
}

// --- Sortable Task Card ---

function SortableTaskCard({
  task,
  isInflight,
  onClick,
  onApprove,
  onStatusChange,
}: {
  task: Task;
  isInflight: boolean;
  onClick: (task: Task) => void;
  onApprove: (taskId: string) => void;
  onStatusChange: (taskId: string, newStatus: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: task.id,
    disabled: isInflight,
  });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : isInflight ? 0.6 : 1,
  };

  const shortId = task.id.slice(-4);

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      className={`bg-pir-surface-1 border border-pir rounded px-3 py-2 cursor-grab active:cursor-grabbing border-l-2 ${PRIORITY_COLORS[task.priority] || ""} hover:border-pir-accent transition-colors`}
    >
      {/* Title row */}
      <div className="flex items-start justify-between gap-1">
        <div
          className="text-label text-pir-text-primary truncate flex-1 cursor-pointer"
          onClick={(e) => {
            if (!isDragging) {
              e.stopPropagation();
              onClick(task);
            }
          }}
        >
          {task.title}
        </div>
        <span className="text-caption text-pir-text-muted shrink-0">#{shortId}</span>
      </div>

      {/* Tags + priority */}
      <div className="flex items-center gap-1 mt-1 flex-wrap">
        {task.tags.slice(0, 2).map((tag) => (
          <span key={tag} className={`text-[9px] px-1 rounded ${getTagColor(tag)}`}>
            {tag}
          </span>
        ))}
        {task.priority && (
          <span className={`text-[9px] px-1 rounded ${PRIORITY_BADGE_COLORS[task.priority] || ""}`}>
            {PRIORITY_BADGES[task.priority] || task.priority}
          </span>
        )}
        {task.pr_status && (() => {
          const b = PR_BADGE[task.pr_status];
          return b ? (
            <span className={`text-[9px] px-1 rounded ${b.cls}`}>
              {b.label}
            </span>
          ) : null;
        })()}
        {task.owner_id && (
          <span className="text-[9px] text-pir-text-muted ml-auto truncate max-w-[80px]">{task.owner_id}</span>
        )}
      </div>

      {/* Actions row */}
      <div
        className="flex items-center gap-1 mt-1.5"
        onClick={(e) => e.stopPropagation()}
        onPointerDown={(e) => e.stopPropagation()}
      >
        {task.status === "pending" && (
          <button
            onClick={() => onApprove(task.id)}
            className="text-[10px] px-2 py-0.5 bg-pir-accent/20 text-pir-accent rounded hover:bg-pir-accent/30 transition-colors"
          >
            Approve
          </button>
        )}
        <select
          value={task.status}
          onChange={(e) => onStatusChange(task.id, e.target.value)}
          className="ml-auto text-[9px] bg-pir-surface-0 border border-pir rounded px-1 py-0.5 text-pir-text-muted cursor-pointer"
        >
          {ALL_STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>
    </div>
  );
}

// --- Drag Overlay Card ---

function TaskCardOverlay({ task }: { task: Task }) {
  return (
    <div className={`bg-pir-surface-1 border border-pir-accent rounded px-3 py-2 shadow-lg border-l-2 ${PRIORITY_COLORS[task.priority] || ""}`}>
      <div className="text-label text-pir-text-primary truncate">{task.title}</div>
    </div>
  );
}

// --- New Task Form ---

function NewTaskForm({
  slug,
  onCreated,
  onCancel,
}: {
  slug: string;
  onCreated: (task: Task) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState("medium");
  const [tagsInput, setTagsInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    setSubmitting(true);
    setError(null);
    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    try {
      const res = await fetch(`${API_BASE_URL}/api/v1/tasks`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: slug, title: title.trim(), priority, tags, status: "pending" }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const created: Task = await res.json();
      onCreated(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create task");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-pir-surface-0 border border-pir-accent rounded p-3 space-y-2"
    >
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Task title"
        required
        className="w-full bg-pir-surface-1 border border-pir rounded px-2 py-1.5 text-label text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent"
      />
      <div className="flex gap-2">
        <select
          value={priority}
          onChange={(e) => setPriority(e.target.value)}
          className="flex-1 bg-pir-surface-1 border border-pir rounded px-2 py-1.5 text-label text-pir-text-secondary focus:outline-none focus:border-pir-accent"
        >
          <option value="high">High</option>
          <option value="medium">Medium</option>
          <option value="low">Low</option>
        </select>
        <input
          type="text"
          value={tagsInput}
          onChange={(e) => setTagsInput(e.target.value)}
          placeholder="Tags (comma-separated)"
          className="flex-[2] bg-pir-surface-1 border border-pir rounded px-2 py-1.5 text-label text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent"
        />
      </div>
      {error && <ErrorAlert message={error} />}
      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1.5 text-label text-pir-text-secondary border border-pir rounded hover:bg-pir-surface-1 transition-colors"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting}
          className="px-3 py-1.5 text-label bg-pir-accent text-white rounded hover:bg-pir-accent/80 disabled:opacity-50 transition-colors"
        >
          {submitting ? "Creating..." : "Create"}
        </button>
      </div>
    </form>
  );
}

// --- List View ---

function ListView({
  tasks,
  onTaskClick,
  onApprove,
  onStatusChange,
}: {
  tasks: Task[];
  onTaskClick: (task: Task) => void;
  onApprove: (taskId: string) => void;
  onStatusChange: (taskId: string, newStatus: string) => void;
}) {
  return (
    <div className="bg-pir-surface-0 border border-pir rounded overflow-hidden">
      <table className="w-full text-label">
        <thead>
          <tr className="border-b border-pir bg-pir-surface-1">
            <th className="text-left px-3 py-2 text-caption text-pir-text-muted font-medium w-16">#</th>
            <th className="text-left px-3 py-2 text-caption text-pir-text-muted font-medium">Title</th>
            <th className="text-left px-3 py-2 text-caption text-pir-text-muted font-medium w-36">Status</th>
            <th className="text-left px-3 py-2 text-caption text-pir-text-muted font-medium w-20">Priority</th>
            <th className="text-left px-3 py-2 text-caption text-pir-text-muted font-medium w-32">Tags</th>
            <th className="text-left px-3 py-2 text-caption text-pir-text-muted font-medium w-28">Assigned</th>
            <th className="text-left px-3 py-2 text-caption text-pir-text-muted font-medium w-24">Action</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr
              key={task.id}
              className="border-b border-pir hover:bg-pir-surface-1 cursor-pointer"
            >
              <td
                className="px-3 py-2 text-caption text-pir-text-muted"
                onClick={() => onTaskClick(task)}
              >
                #{task.id.slice(-4)}
              </td>
              <td
                className="px-3 py-2 text-pir-text-primary"
                onClick={() => onTaskClick(task)}
              >
                {task.title}
              </td>
              <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
                <select
                  value={task.status}
                  onChange={(e) => onStatusChange(task.id, e.target.value)}
                  className="bg-pir-surface-0 border border-pir rounded px-1 py-0.5 text-caption text-pir-text-secondary w-full"
                >
                  {ALL_STATUSES.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </td>
              <td
                className="px-3 py-2"
                onClick={() => onTaskClick(task)}
              >
                <span className={`text-caption px-1.5 py-0.5 rounded ${PRIORITY_BADGE_COLORS[task.priority] || "text-pir-text-muted"}`}>
                  {PRIORITY_BADGES[task.priority] || task.priority}
                </span>
              </td>
              <td
                className="px-3 py-2"
                onClick={() => onTaskClick(task)}
              >
                <div className="flex flex-wrap gap-1">
                  {task.tags.slice(0, 2).map((tag) => (
                    <span key={tag} className={`text-[9px] px-1 rounded ${getTagColor(tag)}`}>
                      {tag}
                    </span>
                  ))}
                </div>
              </td>
              <td
                className="px-3 py-2 text-caption text-pir-text-muted truncate"
                onClick={() => onTaskClick(task)}
              >
                {task.owner_id || "-"}
              </td>
              <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
                {task.status === "pending" && (
                  <button
                    onClick={() => onApprove(task.id)}
                    className="text-[10px] px-2 py-0.5 bg-pir-accent/20 text-pir-accent rounded hover:bg-pir-accent/30 transition-colors"
                  >
                    Approve
                  </button>
                )}
              </td>
            </tr>
          ))}
          {tasks.length === 0 && (
            <tr>
              <td colSpan={7} className="px-3 py-6 text-center text-caption text-pir-text-muted">
                No tasks
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// --- Main KanbanBoard ---

export default function KanbanBoard({ slug }: { slug: string }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [draggedTask, setDraggedTask] = useState<Task | null>(null);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [showNewTaskForm, setShowNewTaskForm] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [sortBy, setSortBy] = useState<"newest" | "priority">("newest");
  const [viewMode, setViewMode] = useState<"board" | "list">("board");
  const inflightTasks = useRef(new Set<string>());

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_BASE_URL}/api/v1/tasks?project=${encodeURIComponent(slug)}&limit=100`, {
      credentials: "include",
      signal: controller.signal,
    })
      .then((r) => r.json())
      .then((data) => setTasks(Array.isArray(data) ? data : []))
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [slug]);

  const handleDragStart = useCallback((event: DragStartEvent) => {
    const task = tasks.find((t) => t.id === event.active.id);
    setDraggedTask(task || null);
  }, [tasks]);

  const handleDragEnd = useCallback(async (event: DragEndEvent) => {
    setDraggedTask(null);
    const { active, over } = event;
    if (!over) return;

    const taskId = active.id as string;
    let newStatus = over.id as string;
    if (!COLUMNS.includes(newStatus as Column)) {
      const overTask = tasks.find((t) => t.id === over.id);
      if (overTask) newStatus = overTask.status;
    }

    const task = tasks.find((t) => t.id === taskId);
    if (!task || task.status === newStatus) return;
    if (inflightTasks.current.has(taskId)) return;

    const prevTasks = [...tasks];
    inflightTasks.current.add(taskId);
    setTasks((prev) => prev.map((t) => (t.id === taskId ? { ...t, status: newStatus as Task["status"] } : t)));

    try {
      const res = await fetch(`${API_BASE_URL}/api/v1/tasks/${taskId}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) setTasks(prevTasks);
    } catch {
      setTasks(prevTasks);
    } finally {
      inflightTasks.current.delete(taskId);
    }
  }, [tasks]);

  const handleApprove = useCallback(async (taskId: string) => {
    if (inflightTasks.current.has(taskId)) return;
    const prevTasks = [...tasks];
    inflightTasks.current.add(taskId);
    setTasks((prev) => prev.map((t) => (t.id === taskId ? { ...t, status: "approved" } : t)));
    try {
      const res = await fetch(`${API_BASE_URL}/api/v1/tasks/${taskId}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "approved" }),
      });
      if (!res.ok) setTasks(prevTasks);
    } catch {
      setTasks(prevTasks);
    } finally {
      inflightTasks.current.delete(taskId);
    }
  }, [tasks]);

  const handleStatusChange = useCallback(async (taskId: string, newStatus: string) => {
    if (inflightTasks.current.has(taskId)) return;
    const prevTasks = [...tasks];
    inflightTasks.current.add(taskId);
    setTasks((prev) => prev.map((t) => (t.id === taskId ? { ...t, status: newStatus as Task["status"] } : t)));
    try {
      const res = await fetch(`${API_BASE_URL}/api/v1/tasks/${taskId}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) setTasks(prevTasks);
    } catch {
      setTasks(prevTasks);
    } finally {
      inflightTasks.current.delete(taskId);
    }
  }, [tasks]);

  const handleTaskCreated = useCallback((task: Task) => {
    setTasks((prev) => [task, ...prev]);
    setShowNewTaskForm(false);
  }, []);

  const handleTaskUpdated = useCallback((updated: Task) => {
    setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
  }, []);

  const handleTaskDeleted = useCallback((taskId: string) => {
    setTasks((prev) => prev.filter((t) => t.id !== taskId));
    setSelectedTask(null);
  }, []);

  if (loading) return <div className="text-pir-text-muted text-label p-4">Loading tasks...</div>;

  // Filter + sort
  const PRIORITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };
  const filteredTasks = tasks
    .filter((t) => statusFilter === "all" || t.status === statusFilter)
    .sort((a, b) => {
      if (sortBy === "priority") {
        return (PRIORITY_ORDER[a.priority] ?? 3) - (PRIORITY_ORDER[b.priority] ?? 3);
      }
      return 0; // newest = insertion order (API returns newest first)
    });

  const tasksByColumn: Record<string, Task[]> = {};
  for (const col of COLUMNS) {
    tasksByColumn[col] = filteredTasks.filter((t) => t.status === col);
  }
  const failedTasks = tasks.filter((t) => t.status === "failed");

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={() => setShowNewTaskForm((v) => !v)}
          className="px-3 py-1.5 text-label bg-pir-accent text-white rounded hover:bg-pir-accent/80 transition-colors shrink-0"
        >
          New Task
        </button>

        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="bg-pir-surface-0 border border-pir rounded px-2 py-1.5 text-label text-pir-text-secondary focus:outline-none focus:border-pir-accent"
        >
          <option value="all">All status</option>
          {ALL_STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>

        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as "newest" | "priority")}
          className="bg-pir-surface-0 border border-pir rounded px-2 py-1.5 text-label text-pir-text-secondary focus:outline-none focus:border-pir-accent"
        >
          <option value="newest">Newest</option>
          <option value="priority">Priority</option>
        </select>

        <div className="ml-auto border border-pir rounded overflow-hidden flex shrink-0">
          <button
            onClick={() => setViewMode("board")}
            className={`px-3 py-1.5 text-label transition-colors ${viewMode === "board" ? "bg-pir-surface-1 text-pir-text-primary" : "bg-pir-surface-0 text-pir-text-muted hover:bg-pir-surface-1"}`}
          >
            Board
          </button>
          <button
            onClick={() => setViewMode("list")}
            className={`px-3 py-1.5 text-label transition-colors border-l border-pir ${viewMode === "list" ? "bg-pir-surface-1 text-pir-text-primary" : "bg-pir-surface-0 text-pir-text-muted hover:bg-pir-surface-1"}`}
          >
            List
          </button>
        </div>
      </div>

      {/* New task form */}
      {showNewTaskForm && (
        <NewTaskForm
          slug={slug}
          onCreated={handleTaskCreated}
          onCancel={() => setShowNewTaskForm(false)}
        />
      )}

      {/* Board or List view */}
      {viewMode === "board" ? (
        <DndContext collisionDetection={closestCorners} onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
          <div className="flex flex-col md:flex-row gap-2 md:overflow-x-auto pb-2">
            {COLUMNS.map((col) => (
              <DroppableColumn key={col} id={col}>
                <SortableContext items={tasksByColumn[col].map((t) => t.id)} strategy={verticalListSortingStrategy}>
                  {tasksByColumn[col].map((task) => (
                    <SortableTaskCard
                      key={task.id}
                      task={task}
                      isInflight={inflightTasks.current.has(task.id)}
                      onClick={setSelectedTask}
                      onApprove={handleApprove}
                      onStatusChange={handleStatusChange}
                    />
                  ))}
                </SortableContext>
              </DroppableColumn>
            ))}
          </div>
          <DragOverlay>
            {draggedTask && <TaskCardOverlay task={draggedTask} />}
          </DragOverlay>
        </DndContext>
      ) : (
        <ListView
          tasks={filteredTasks}
          onTaskClick={setSelectedTask}
          onApprove={handleApprove}
          onStatusChange={handleStatusChange}
        />
      )}

      {/* Task detail modal */}
      {selectedTask && (
        <TaskDetailModal
          task={selectedTask}
          slug={slug}
          onClose={() => setSelectedTask(null)}
          onStatusChange={(taskId, newStatus) => {
            setTasks((prev) =>
              prev.map((t) => (t.id === taskId ? { ...t, status: newStatus as Task["status"] } : t))
            );
            setSelectedTask(null);
          }}
          onTaskUpdated={handleTaskUpdated}
          onTaskDeleted={handleTaskDeleted}
        />
      )}

      {/* Failed tasks section */}
      {failedTasks.length > 0 && (
        <div>
          <h4 className="text-caption font-medium text-pir-error mb-2">Failed Tasks</h4>
          <div className="space-y-1">
            {failedTasks.map((task) => (
              <div key={task.id} className="flex items-center gap-2 bg-pir-surface-1 border border-pir-error/30 rounded px-3 py-2">
                <span className="text-label text-pir-text-secondary truncate">{task.title}</span>
                <button
                  onClick={() => handleStatusChange(task.id, "approved")}
                  className="ml-auto text-[10px] text-pir-accent hover:text-pir-accent/80 shrink-0"
                >
                  Retry
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
