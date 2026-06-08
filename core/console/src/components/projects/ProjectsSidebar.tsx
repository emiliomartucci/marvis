"use client";

import { useCallback, useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { getPrograms } from "@/lib/api";
import type { ProgramInfo, ProjectInfo, ProjectType } from "@/lib/types";
import { useDesignV2 } from "@/lib/useDesignV2";
import ProjectsSidebarV2 from "./ProjectsSidebarV2";

const TYPE_BADGE: Record<ProjectType, { label: string; className: string }> = {
  work:   { label: "W", className: "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400" },
  code:   { label: "C", className: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400" },
  system: { label: "S", className: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400" },
};

const STATUS_DOT: Record<string, string> = {
  active: "bg-pir-success",
  paused: "bg-pir-warning",
  blocked: "bg-pir-error",
  completed: "bg-pir-text-muted",
  not_started: "bg-pir-text-muted/50",
};

function ProjectRow({ project, isActive, onClick }: {
  project: ProjectInfo;
  isActive: boolean;
  onClick: () => void;
}) {
  const activeTasks = project.task_counts.pending + project.task_counts.approved + project.task_counts.in_progress;
  const totalTasks = activeTasks + project.task_counts.completed + project.task_counts.rejected + project.task_counts.failed;

  return (
    <button
      onClick={onClick}
      data-active={isActive}
      className={`w-full flex items-center gap-2 px-2 py-1 text-left rounded transition-colors group ${
        isActive
          ? "bg-pir-surface-2 border-l-2 border-l-pir-accent"
          : "hover:bg-pir-surface-1 border-l-2 border-l-transparent"
      } ${project.lifecycle === "archived" ? "opacity-40" : ""}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${STATUS_DOT[project.status || ""] || "bg-pir-text-muted/50"}`} />
      {project.type && (
        <span className={`text-[9px] font-bold leading-none px-1 py-0.5 rounded shrink-0 ${TYPE_BADGE[project.type]?.className || "bg-gray-100 text-gray-600"}`}>
          {TYPE_BADGE[project.type]?.label || project.type[0].toUpperCase()}
        </span>
      )}
      <span className={`text-caption font-medium truncate ${isActive ? "text-pir-text-primary" : "text-pir-text-secondary"}`}>
        {project.slug}
      </span>
      {totalTasks > 0 && (
        <span className="ml-auto text-caption tabular-nums text-pir-text-tertiary shrink-0">
          {activeTasks}/{totalTasks}
        </span>
      )}
    </button>
  );
}

export default function ProjectsSidebar() {
  const v2 = useDesignV2();
  if (v2) return <ProjectsSidebarV2 />;
  return <ProjectsSidebarV1 />;
}

function ProjectsSidebarV1() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeSlug = searchParams.get("slug");

  const [programs, setPrograms] = useState<ProgramInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState(() => searchParams.get("q") ?? "");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() => {
    if (typeof window === "undefined") return {};
    try {
      return JSON.parse(localStorage.getItem("pir-sidebar-collapsed") || "{}");
    } catch {
      return {};
    }
  });
  const [showOffServer, setShowOffServer] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem("pir-show-off-server") === "true";
  });

  useEffect(() => {
    const controller = new AbortController();
    getPrograms({ signal: controller.signal })
      .then(setPrograms)
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    localStorage.setItem("pir-sidebar-collapsed", JSON.stringify(collapsed));
  }, [collapsed]);

  useEffect(() => {
    localStorage.setItem("pir-show-off-server", showOffServer ? "true" : "false");
  }, [showOffServer]);

  const toggleProgram = useCallback((name: string) => {
    setCollapsed((prev) => ({ ...prev, [name]: !prev[name] }));
  }, []);

  function navigateToProject(slug: string) {
    router.push(`/projects/detail/?slug=${encodeURIComponent(slug)}`);
  }

  const filteredPrograms = programs
    .map((prog) => ({
      ...prog,
      projects: prog.projects.filter((p) => {
        if (!showOffServer && !p.on_server) return false;
        if (search && !p.slug.toLowerCase().includes(search.toLowerCase())) return false;
        return true;
      }),
    }))
    .filter((prog) => prog.projects.length > 0);

  const offServerCount = programs.reduce(
    (sum, prog) => sum + prog.projects.filter((p) => !p.on_server).length,
    0
  );

  return (
    <div className="flex flex-col h-full py-2">
      {/* Search */}
      <div className="px-3 mb-2">
        <input
          type="text"
          placeholder="Search projects..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full bg-pir-surface-1 border border-pir rounded px-2 py-1 text-caption text-pir-text-primary placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent transition-colors"
        />
      </div>

      {/* Program tree */}
      <div className="flex-1 overflow-y-auto px-2 space-y-1">
        {loading && (
          <div className="px-2 text-caption text-pir-text-muted">Loading...</div>
        )}
        {filteredPrograms.map((prog) => (
          <div key={prog.name}>
            <button
              onClick={() => toggleProgram(prog.name)}
              className="w-full flex items-center gap-1.5 px-1 py-1 hover:bg-pir-surface-1 rounded transition-colors group"
            >
              <svg
                className={`w-3 h-3 text-pir-text-muted transition-transform ${collapsed[prog.name] ? "" : "rotate-90"}`}
                viewBox="0 0 12 12"
                fill="currentColor"
              >
                <path d="M4 2l4 4-4 4z" />
              </svg>
              <span className="text-caption font-semibold uppercase tracking-wider text-pir-text-muted">
                {prog.name}
              </span>
              <span className="text-caption text-pir-text-muted/50 ml-auto">
                {prog.projects.length}
              </span>
            </button>
            {!collapsed[prog.name] && (
              <div className="ml-2 space-y-px">
                {prog.projects.map((project) => (
                  <ProjectRow
                    key={project.slug}
                    project={project}
                    isActive={project.slug === activeSlug}
                    onClick={() => navigateToProject(project.slug)}
                  />
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Off-server toggle */}
      {offServerCount > 0 && (
        <div className="px-3 pt-2 border-t border-pir">
          <button
            onClick={() => setShowOffServer(!showOffServer)}
            className={`text-caption transition-colors ${
              showOffServer ? "text-pir-accent" : "text-pir-text-muted hover:text-pir-text-secondary"
            }`}
          >
            {showOffServer ? "Hide" : "Show"} off-server ({offServerCount})
          </button>
        </div>
      )}
    </div>
  );
}
