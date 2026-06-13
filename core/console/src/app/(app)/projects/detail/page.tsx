"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import ProjectsSidebar from "@/components/projects/ProjectsSidebar";
import OverviewTab from "@/components/projects/OverviewTab";
import KanbanBoard from "@/components/projects/KanbanBoard";
import HandoffsTab from "@/components/projects/HandoffsTab";
import DocsTab from "@/components/projects/DocsTab";
import GitGraph from "@/components/projects/GitGraph";
import CostsTab from "@/components/projects/CostsTab";
import BillingTab from "@/components/projects/BillingTab";
import RaciTab from "@/components/projects/RaciTab";
import ProjectSearch from "@/components/projects/ProjectSearch";
import ProjectsSubbar from "@/components/projects/ProjectsSubbar";
import ProjectMainFlow from "@/components/projects/ProjectMainFlow";
import LocalProjectsSurface from "@/components/projects/local/LocalProjectsSurface";
import { getProjectDetail, getProjectCosts, getPrograms } from "@/lib/api";
import type { ProgramInfo, ProjectDetail, ConversationCost } from "@/lib/types";

function countOnServerProjects(programs: ProgramInfo[]): number {
  let total = 0;
  for (const prog of programs) {
    for (const p of prog.projects) {
      if (p.on_server) total += 1;
    }
  }
  return total;
}

function sumCostInWindow(rows: ConversationCost[], windowMs: number): number {
  const cutoff = Date.now() - windowMs;
  let sum = 0;
  for (const r of rows) {
    const ts = new Date(r.created_at || r.completed_at || r.updated_at || "").getTime();
    if (Number.isFinite(ts) && ts >= cutoff) sum += r.cost_usd;
  }
  return sum;
}
import { ErrorAlert } from "@/components/ui/ErrorAlert";
import { useDesignV2 } from "@/lib/useDesignV2";

const TABS = ["overview", "tasks", "handoffs", "docs", "git", "costs", "billing", "raci"] as const;
type Tab = (typeof TABS)[number];

function isLocalMode(): boolean {
  const value = process.env.NEXT_PUBLIC_LOCAL_MODE;
  return value === "1" || value === "true";
}

const TAB_LABELS: Record<Tab, string> = {
  overview: "Overview",
  tasks: "Tasks",
  handoffs: "Handoffs",
  docs: "Docs",
  git: "Git",
  costs: "Costs",
  billing: "Billing",
  raci: "RACI",
};

function ProjectDetailContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const slug = searchParams.get("slug");
  const v2 = useDesignV2();

  const tabParam = searchParams.get("tab") as Tab | null;
  const activeTab: Tab = tabParam && TABS.includes(tabParam) ? tabParam : "overview";

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // v2 subbar metadata
  const [totalProjects, setTotalProjects] = useState<number>(0);
  const [cost7d, setCost7d] = useState<number | null>(null);

  useEffect(() => {
    if (!slug) {
      setLoading(false);
      setError("No project specified");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getProjectDetail(slug, { signal: controller.signal })
      .then(setProject)
      .catch((err) => {
        if (err.name !== "AbortError") setError(err.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [slug]);

  // v2-only: fetch totalProjects + cost 7d for subbar pills.
  useEffect(() => {
    if (!v2 || !slug) return;
    const ctrl = new AbortController();
    getPrograms({ signal: ctrl.signal })
      .then((programs) => setTotalProjects(countOnServerProjects(programs)))
      .catch(() => {});
    getProjectCosts(slug, {}, { signal: ctrl.signal })
      .then((rows) => setCost7d(sumCostInWindow(rows, 7 * 24 * 3600 * 1000)))
      .catch(() => setCost7d(null));
    return () => ctrl.abort();
  }, [v2, slug]);

  const tabsToShow = useMemo(
    () => TABS.filter((tab) => tab !== "git" || project?.type !== "work"),
    [project?.type]
  );

  function setTab(tab: Tab) {
    const params = new URLSearchParams();
    params.set("slug", slug!);
    if (tab !== "overview") params.set("tab", tab);
    router.push(`/projects/detail/?${params.toString()}`);
  }

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <span className="text-pir-text-muted text-body">Loading project...</span>
      </div>
    );
  }

  if (error || !project) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3">
        <ErrorAlert message={error || "Project not found"} />
        <button
          onClick={() => router.push("/projects/")}
          className="text-caption text-pir-accent hover:text-pir-accent/80"
        >
          Back to projects
        </button>
      </div>
    );
  }

  // --- v2 single-pager render ---
  if (v2) {
    return (
      <div className="h-full flex flex-col overflow-hidden">
        <ProjectsSubbar
          project={project}
          totalProjects={totalProjects}
          cost7d={cost7d}
        />
        <ProjectMainFlow project={project} />
      </div>
    );
  }

  // --- v1 tabbed render (unchanged) ---
  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Header: project name + tabs */}
      <div className="shrink-0 border-b border-pir px-4 py-3">
        <div className="flex items-center gap-2 mb-2">
          <h1 className="text-heading text-pir-text-primary truncate">{project.name || slug}</h1>
          {project.program && (
            <span className="text-caption text-pir-text-muted bg-pir-surface-1 px-2 py-0.5 rounded">
              {project.program}
            </span>
          )}
          <div className="ml-auto">
            <ProjectSearch slug={slug!} />
          </div>
        </div>

        {/* Tab bar */}
        <div className="flex gap-1 overflow-x-auto scrollbar-hide -mx-4 px-4 md:mx-0 md:px-0">
          {tabsToShow.map((tab) => (
            <button
              key={tab}
              onClick={() => setTab(tab)}
              className={`px-3 py-1.5 text-label rounded transition-colors whitespace-nowrap shrink-0 ${
                activeTab === tab
                  ? "text-pir-accent bg-pir-surface-2"
                  : "text-pir-text-muted hover:text-pir-text-secondary"
              }`}
            >
              {TAB_LABELS[tab]}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content — full width */}
      <div className="flex-1 overflow-y-auto p-4">
        {activeTab === "overview" && <OverviewTab project={project} />}
        {activeTab === "tasks" && <KanbanBoard slug={slug!} />}
        {activeTab === "handoffs" && <HandoffsTab slug={slug!} />}
        {activeTab === "docs" && <DocsTab slug={slug!} />}
        {activeTab === "git" && <GitGraph slug={slug!} />}
        {activeTab === "costs" && <CostsTab slug={slug!} />}
        {activeTab === "billing" && <BillingTab slug={slug!} />}
        {activeTab === "raci" && <RaciTab slug={slug!} />}
      </div>
    </div>
  );
}

export default function ProjectDetailPage() {
  if (isLocalMode()) return <LocalProjectsSurface />;

  return (
    <div className="flex flex-1 min-h-0 h-full">
      <aside className="hidden md:flex w-[240px] shrink-0 flex-col bg-pir-surface-0 border-r border-pir overflow-y-auto">
        <Suspense fallback={null}><ProjectsSidebar /></Suspense>
      </aside>
      <div className="flex-1 overflow-hidden">
        <Suspense fallback={null}>
          <ProjectDetailContent />
        </Suspense>
      </div>
    </div>
  );
}
