"use client";

import { Suspense } from "react";
import ProjectsSidebar from "@/components/projects/ProjectsSidebar";
import LocalProjectsSurface from "@/components/projects/local/LocalProjectsSurface";

function isLocalMode(): boolean {
  const value = process.env.NEXT_PUBLIC_LOCAL_MODE;
  return value === "1" || value === "true";
}

function ProjectsLanding() {
  return (
    <div className="h-full flex items-center justify-center">
      <div className="text-center space-y-2">
        <div className="text-heading text-pir-text-primary">Projects</div>
        <div className="text-body text-pir-text-tertiary">
          Select a project from the sidebar to view details.
        </div>
      </div>
    </div>
  );
}

export default function ProjectsPage() {
  if (isLocalMode()) return <LocalProjectsSurface />;

  return (
    <div className="flex flex-1 min-h-0 h-full">
      <aside className="hidden md:flex w-[240px] shrink-0 flex-col bg-pir-surface-0 border-r border-pir overflow-y-auto">
        <Suspense fallback={null}><ProjectsSidebar /></Suspense>
      </aside>
      <div className="flex-1 overflow-hidden">
        <ProjectsLanding />
      </div>
    </div>
  );
}
