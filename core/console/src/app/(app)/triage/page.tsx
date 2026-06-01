"use client";

// v1.2.0 - 2026-04-22 - theme-v2 polish: toolbar h-36 + group/sort buttons, board
// padding 14px, aside bg-surface-0 (gated behind .theme-v2)
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import TriageSidebar from "@/components/triage/TriageSidebar";
import TriageKanban from "@/components/triage/TriageKanban";
import TaskDetailModal from "@/components/projects/TaskDetailModal";
import { filterTriageTasks, loadTriageTasks } from "@/lib/triage";
import { useDesignV2 } from "@/lib/useDesignV2";
import type { TaskResponse, TriageFilters } from "@/lib/types";

const DEFAULT_FILTERS: TriageFilters = {
  status: [],
  kind: [],
  project: [],
  priority: [],
  delegation: [],
};

function TriageBoard() {
  const v2 = useDesignV2();
  const searchParams = useSearchParams();
  const preSelectTaskId = searchParams.get("task");

  const [allTasks, setAllTasks] = useState<TaskResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<TriageFilters>(DEFAULT_FILTERS);
  const [selectedTask, setSelectedTask] = useState<TaskResponse | null>(null);
  const refreshRequestIdRef = useRef(0);

  const refreshTasks = useCallback(
    async ({ showLoading = false }: { showLoading?: boolean } = {}) => {
      const requestId = ++refreshRequestIdRef.current;
      if (showLoading) setLoading(true);

      try {
        const data = await loadTriageTasks();
        if (refreshRequestIdRef.current !== requestId) return;

        setAllTasks(data);
        if (preSelectTaskId) {
          const found = data.find((t) => t.id === preSelectTaskId);
          if (found) setSelectedTask(found);
        }
      } catch {
        // Keep the existing board state on silent refresh failures.
      } finally {
        if (showLoading && refreshRequestIdRef.current === requestId) {
          setLoading(false);
        }
      }
    },
    [preSelectTaskId]
  );

  // Fetch all triage-relevant tasks and refresh when the tab becomes active again.
  useEffect(() => {
    void refreshTasks({ showLoading: true });

    const handleVisibility = () => {
      if (!document.hidden) {
        void refreshTasks();
      }
    };
    const handleFocus = () => {
      void refreshTasks();
    };
    const handlePageShow = () => {
      void refreshTasks();
    };

    document.addEventListener("visibilitychange", handleVisibility);
    window.addEventListener("focus", handleFocus);
    window.addEventListener("pageshow", handlePageShow);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("focus", handleFocus);
      window.removeEventListener("pageshow", handlePageShow);
    };
  }, [refreshTasks]);

  // Derive unique project list from tasks
  const projects = useMemo(() => {
    const set = new Set(allTasks.map((t) => t.project));
    return Array.from(set).sort();
  }, [allTasks]);

  // Apply client-side filters
  const filteredTasks = useMemo(() => {
    return filterTriageTasks(allTasks, filters);
  }, [allTasks, filters]);

  const handleTaskClick = useCallback((task: TaskResponse) => {
    setSelectedTask(task);
  }, []);

  const handleTaskUpdated = useCallback((updated: TaskResponse) => {
    setAllTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
    setSelectedTask(null);
  }, []);

  const handleTaskDeleted = useCallback((taskId: string) => {
    setAllTasks((prev) => prev.filter((t) => t.id !== taskId));
    setSelectedTask(null);
  }, []);

  if (loading) {
    return (
      <div className="flex flex-1 min-h-0 h-full">
        <aside className="hidden md:flex w-[240px] shrink-0 flex-col bg-pir-surface-0 border-r border-pir overflow-y-auto">
          <TriageSidebar
            tasks={allTasks}
            filters={filters}
            projects={projects}
            onFiltersChange={setFilters}
          />
        </aside>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-pir-text-muted text-label">Loading tasks...</div>
        </div>
      </div>
    );
  }

  const hasFilters =
    filters.status.length > 0 ||
    filters.kind.length > 0 ||
    filters.project.length > 0 ||
    filters.priority.length > 0 ||
    filters.delegation.length > 0;

  return (
    <div className="flex flex-1 min-h-0 h-full">
      <aside className="hidden md:flex w-[240px] shrink-0 flex-col bg-pir-surface-0 border-r border-pir overflow-y-auto">
        <TriageSidebar
          tasks={allTasks}
          filters={filters}
          projects={projects}
          onFiltersChange={setFilters}
        />
      </aside>
      <div className="flex-1 flex flex-col overflow-hidden bg-pir-base">
        {/* Toolbar */}
        {v2 ? (
          <div
            className="shrink-0 flex items-center bg-pir-surface-0 border-b border-pir"
            style={{ height: 36, padding: "0 16px", gap: 12 }}
          >
            <span
              className="text-pir-text-secondary"
              style={{
                fontFamily: "var(--pir-font-sans)",
                fontWeight: 500,
                fontSize: "11px",
                lineHeight: 1,
              }}
            >
              <b className="text-pir-text-primary" style={{ fontWeight: 700 }}>
                {filteredTasks.length}
              </b>{" "}
              tasks
            </span>
            {hasFilters && (
              <span
                className="text-pir-text-muted"
                style={{
                  fontFamily: "var(--pir-font-mono)",
                  fontWeight: 500,
                  fontSize: "10px",
                  lineHeight: 1,
                }}
              >
                (filtered from {allTasks.length})
              </span>
            )}
            <span className="flex-1" />
            {/* Grouping state wiring is a follow-up PR — render-only now to match kit spec. */}
            <button
              type="button"
              disabled
              className="text-pir-text-tertiary bg-transparent border border-pir hover:border-pir-strong hover:text-pir-text-primary uppercase disabled:opacity-50 disabled:cursor-not-allowed"
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 600,
                fontSize: "10px",
                lineHeight: 1,
                letterSpacing: "0.1em",
                padding: "5px 9px",
                borderRadius: 2,
              }}
              title="Group by project (coming soon)"
            >
              Group by project
            </button>
            {/* Sort state wiring is a follow-up PR — current order is already ICE desc server-side. */}
            <button
              type="button"
              disabled
              className="text-pir-text-tertiary bg-transparent border border-pir hover:border-pir-strong hover:text-pir-text-primary uppercase disabled:opacity-50 disabled:cursor-not-allowed"
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontWeight: 600,
                fontSize: "10px",
                lineHeight: 1,
                letterSpacing: "0.1em",
                padding: "5px 9px",
                borderRadius: 2,
              }}
              title="Sort: ICE ↓ (default)"
            >
              Sort: ICE ↓
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-3 px-4 py-2 border-b border-pir shrink-0">
            <span className="text-label text-pir-text-secondary">
              {filteredTasks.length} tasks
            </span>
            {hasFilters ? (
              <span className="text-caption text-pir-text-muted">
                (filtered from {allTasks.length})
              </span>
            ) : null}
          </div>
        )}

        {/* Kanban */}
        <div
          className="flex-1 overflow-auto"
          style={v2 ? { padding: 14 } : undefined}
        >
          <div className={v2 ? "" : "p-4"}>
            <TriageKanban
              tasks={filteredTasks}
              onTasksChange={setAllTasks}
              onTaskClick={handleTaskClick}
            />
          </div>
        </div>

        {/* Task detail modal */}
        {selectedTask && (
          <TaskDetailModal
            task={selectedTask}
            slug={selectedTask.project}
            onClose={() => setSelectedTask(null)}
            onStatusChange={(taskId, newStatus) => {
              setAllTasks((prev) =>
                prev.map((t) =>
                  t.id === taskId ? { ...t, status: newStatus as TaskResponse["status"] } : t
                )
              );
              setSelectedTask(null);
            }}
            onTaskUpdated={handleTaskUpdated}
            onTaskDeleted={handleTaskDeleted}
          />
        )}
      </div>
    </div>
  );
}

export default function TriagePage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center h-full bg-pir-base">
          <div className="text-pir-text-muted text-label">Loading...</div>
        </div>
      }
    >
      <TriageBoard />
    </Suspense>
  );
}
