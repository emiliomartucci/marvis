// v1.0.0 - 2026-04-22 - Main 7-section flow for /projects/detail single-pager v2 (PR #9)
"use client";

import { useCallback, useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import ContextSection from "./sections/ContextSection";
import StatusUpdatesSection from "./sections/StatusUpdatesSection";
import MiniKanbanSection from "./sections/MiniKanbanSection";
import GitRecentSection from "./sections/GitRecentSection";
import DocsSection from "./sections/DocsSection";
import StakeholdersSection from "./sections/StakeholdersSection";
import CostsSummarySection from "./sections/CostsSummarySection";
import HeavyViewModal from "./HeavyViewModal";
import CostsTab from "./CostsTab";
import DocsTab from "./DocsTab";
import GitGraph from "./GitGraph";
import RaciTab from "./RaciTab";
import type { ProjectDetail } from "@/lib/types";

type ModalKind = "costs" | "git" | "docs" | "raci" | null;

const VALID_VIEWS: Record<string, ModalKind> = {
  costs: "costs",
  git: "git",
  docs: "docs",
  raci: "raci",
};

function ProjectMainFlow({ project }: { project: ProjectDetail }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const statusTextareaRef = useRef<HTMLTextAreaElement>(null);

  const viewParam = searchParams.get("view");
  const modal: ModalKind = viewParam ? VALID_VIEWS[viewParam] ?? null : null;

  // Build slug-preserving URLs
  const withView = useCallback(
    (view: ModalKind) => {
      const params = new URLSearchParams();
      params.set("slug", project.slug);
      if (view) params.set("view", view);
      return `/projects/detail/?${params.toString()}`;
    },
    [project.slug]
  );

  const setModal = useCallback(
    (view: ModalKind) => router.push(withView(view)),
    [router, withView]
  );

  // Scroll-to-section driven by ?section=<anchor>
  const sectionParam = searchParams.get("section");
  useEffect(() => {
    if (!sectionParam) return;
    const el = document.getElementById(sectionParam);
    if (el) {
      // Defer to allow sections to mount first.
      setTimeout(() => el.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    }
  }, [sectionParam]);

  // Keyboard shortcuts (Phase 1): c → focus status textarea, ? → alert cheatsheet
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const tag = (target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || target?.isContentEditable) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "c") {
        e.preventDefault();
        statusTextareaRef.current?.focus();
      } else if (e.key === "?") {
        e.preventDefault();
        alert(
          [
            "Keyboard shortcuts (Phase 1):",
            "  c     → focus status update textarea",
            "  g k   → scroll to Kanban",
            "  g d   → scroll to Docs",
            "  g s   → scroll to Status",
            "  g g   → scroll to top",
            "  ?     → this cheatsheet",
          ].join("\n")
        );
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // g X scroll shortcuts (two-key sequence)
  useEffect(() => {
    const GG_MAP: Record<string, string> = {
      k: "kanban",
      d: "docs",
      s: "status",
      g: "context",
    };
    let lastG = 0;
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const tag = (target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || target?.isContentEditable) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const now = Date.now();
      if (e.key === "g") {
        lastG = now;
        return;
      }
      if (now - lastG >= 900) return;
      const anchor = GG_MAP[e.key];
      lastG = 0;
      if (!anchor) return;
      e.preventDefault();
      const el = document.getElementById(anchor);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      else if (anchor === "context") window.scrollTo({ top: 0, behavior: "smooth" });
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  const gitSupported = project.type !== "work";

  return (
    <div className="flex-1 min-w-0 overflow-y-auto bg-pir-base">
      <ContextSection project={project} />
      <StatusUpdatesSection slug={project.slug} textareaRef={statusTextareaRef} />
      <MiniKanbanSection slug={project.slug} />
      <GitRecentSection
        slug={project.slug}
        supported={gitSupported}
        onOpenFull={() => setModal("git")}
      />
      <DocsSection slug={project.slug} onOpenAll={() => setModal("docs")} />
      <StakeholdersSection slug={project.slug} onOpenRaci={() => setModal("raci")} />
      <CostsSummarySection slug={project.slug} onOpenBreakdown={() => setModal("costs")} />

      {modal === "costs" && (
        <HeavyViewModal title={`Costs · ${project.slug}`} onClose={() => setModal(null)}>
          <CostsTab slug={project.slug} />
        </HeavyViewModal>
      )}
      {modal === "git" && gitSupported && (
        <HeavyViewModal title={`Git · ${project.slug}`} onClose={() => setModal(null)}>
          <GitGraph slug={project.slug} />
        </HeavyViewModal>
      )}
      {modal === "docs" && (
        <HeavyViewModal title={`Docs · ${project.slug}`} onClose={() => setModal(null)}>
          <DocsTab slug={project.slug} />
        </HeavyViewModal>
      )}
      {modal === "raci" && (
        <HeavyViewModal title={`RACI · ${project.slug}`} onClose={() => setModal(null)}>
          <RaciTab slug={project.slug} />
        </HeavyViewModal>
      )}
    </div>
  );
}

export default ProjectMainFlow;
