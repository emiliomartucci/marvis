"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { getPrograms } from "@/lib/api";
import { useT } from "@/lib/i18n";
import {
  PROJECT_COLOR_CHANGED_EVENT,
  patchProgramProjectColor,
  projectDisplayName,
  type ProjectColorChangedDetail,
} from "@/lib/projectsLocal";
import type { ProgramInfo, ProjectInfo } from "@/lib/types";

const COLLAPSED_KEY = "marvis:local-project-programs-collapsed";

function onServerProjects(program: ProgramInfo): ProjectInfo[] {
  return program.projects.filter((project) => project.on_server);
}

function loadCollapsed(): Set<string> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) && parsed.every((item) => typeof item === "string")
      ? new Set(parsed)
      : null;
  } catch {
    return null;
  }
}

function saveCollapsed(value: Set<string>): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...value]));
  } catch {
    // Local storage is optional for static export mode.
  }
}

function projectMatches(project: ProjectInfo, query: string): boolean {
  if (!query) return true;
  const haystack = [
    project.slug,
    project.name,
    project.program,
    project.description,
    project.language,
  ].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes(query);
}

function ProjectColorDot({ color }: { color: string | null | undefined }) {
  return (
    <span
      aria-hidden
      className={`h-[7px] w-[7px] shrink-0 rounded-full ${color ? "" : "bg-pir-border-strong"}`}
      style={color ? { backgroundColor: color } : undefined}
    />
  );
}

function SearchIcon() {
  return (
    <svg
      aria-hidden
      className="h-3 w-3 shrink-0 text-pir-text-muted"
      viewBox="0 0 16 16"
      fill="none"
    >
      <circle cx="7" cy="7" r="4" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10.25 10.25 13 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function ClearIcon() {
  return (
    <svg
      aria-hidden
      className="h-3 w-3"
      viewBox="0 0 16 16"
      fill="none"
    >
      <path d="m5 5 6 6M11 5l-6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function PersonalScopeIcon() {
  return (
    <svg
      aria-hidden
      className="h-3 w-3 shrink-0 text-pir-text-muted"
      viewBox="0 0 16 16"
      fill="none"
    >
      <circle cx="8" cy="5" r="2.5" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M3.75 13c.6-2.2 2.05-3.3 4.25-3.3s3.65 1.1 4.25 3.3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ProjectRow({ project, activeSlug }: { project: ProjectInfo; activeSlug: string | null }) {
  const active = project.slug === activeSlug;
  return (
    <Link
      key={project.slug}
      href={`/projects/?slug=${encodeURIComponent(project.slug)}`}
      className={`flex h-[30px] items-center gap-2 rounded px-2 text-caption transition-colors ${
        active
          ? "bg-pir-accent/10 text-pir-text-primary"
          : "text-pir-text-tertiary hover:bg-pir-surface-1 hover:text-pir-text-primary"
      }`}
    >
      <ProjectColorDot color={project.color} />
      <span className="min-w-0 flex-1 truncate text-left">
        {projectDisplayName(project)}
      </span>
      {project.scope === "personal" && <PersonalScopeIcon />}
    </Link>
  );
}

export default function ProjectNavigator() {
  const { t } = useT();
  const strings = t.projects.navigator;
  const searchParams = useSearchParams();
  const activeSlug = searchParams.get("slug");
  const [programs, setPrograms] = useState<ProgramInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string> | null>(() => loadCollapsed());

  useEffect(() => {
    const controller = new AbortController();
    getPrograms({ signal: controller.signal })
      .then((nextPrograms) => {
        setPrograms(nextPrograms);
        setCollapsed((current) => current ?? new Set());
      })
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    function handleColorChanged(event: Event) {
      const detail = (event as CustomEvent<ProjectColorChangedDetail>).detail;
      if (!detail?.slug) return;
      setPrograms((current) => patchProgramProjectColor(current, detail.slug, detail.color));
    }

    window.addEventListener(PROJECT_COLOR_CHANGED_EVENT, handleColorChanged);
    return () => window.removeEventListener(PROJECT_COLOR_CHANGED_EVENT, handleColorChanged);
  }, []);

  const normalizedQuery = query.trim().toLowerCase();
  const programsWithServerProjects = useMemo(() => {
    return programs
      .map((program) => ({ ...program, projects: onServerProjects(program) }))
      .filter((program) => program.projects.length > 0);
  }, [programs]);
  const visiblePrograms = useMemo(() => {
    return programs
      .map((program) => ({
        ...program,
        projects: onServerProjects(program).filter((project) => projectMatches(project, normalizedQuery)),
      }))
      .filter((program) => program.projects.length > 0);
  }, [normalizedQuery, programs]);
  const searchResults = useMemo(
    () => visiblePrograms.flatMap((program) => program.projects),
    [visiblePrograms]
  );

  const totalProjectCount = programsWithServerProjects.reduce(
    (sum, program) => sum + program.projects.length,
    0
  );
  const searching = normalizedQuery.length > 0;

  function toggleProgram(name: string) {
    setCollapsed((current) => {
      const next = new Set(current ?? programs.map((program) => program.name));
      if (next.has(name)) next.delete(name);
      else next.add(name);
      saveCollapsed(next);
      return next;
    });
  }

  return (
    <section className="flex h-full min-h-0 flex-col" aria-label={t.appShell.projectsSlotLabel}>
      <div className="flex items-center justify-between">
        <span className="font-mono text-caption uppercase text-pir-text-muted">
          {t.appShell.projectsSlotLabel}
        </span>
        <span className="font-mono text-caption text-pir-text-muted">
          {totalProjectCount}
        </span>
      </div>

      <div className="mt-3 flex h-8 items-center gap-2 rounded border border-pir bg-pir-base px-2 transition-colors focus-within:border-pir-accent">
        <SearchIcon />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={strings.search}
          aria-label={strings.search}
          className="h-full min-w-0 flex-1 bg-transparent text-caption text-pir-text-primary outline-none placeholder:text-pir-text-muted"
        />
        {query && (
          <button
            type="button"
            onClick={() => setQuery("")}
            className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-pir-text-muted transition-colors hover:bg-pir-surface-1 hover:text-pir-text-primary"
            aria-label="Clear search"
          >
            <ClearIcon />
          </button>
        )}
      </div>

      <div className="mt-3 min-h-0 flex-1 overflow-y-auto pr-1">
        {loading && (
          <div className="text-caption text-pir-text-muted">{strings.loading}</div>
        )}
        {!loading && visiblePrograms.length === 0 && (
          <div className="rounded border border-pir bg-pir-base px-3 py-2 text-caption text-pir-text-muted">
            {strings.empty}
          </div>
        )}
        <div className="flex flex-col gap-1">
          {searching && searchResults.map((project) => (
            <ProjectRow key={project.slug} project={project} activeSlug={activeSlug} />
          ))}
          {!searching && programsWithServerProjects.map((program) => {
            const isCollapsed = !normalizedQuery && (collapsed?.has(program.name) ?? true);
            return (
              <div key={program.name}>
                <button
                  type="button"
                  onClick={() => toggleProgram(program.name)}
                  aria-expanded={!isCollapsed}
                  aria-label={`${isCollapsed ? strings.expand : strings.collapse}: ${program.name}`}
                  className="flex h-8 w-full items-center gap-2 rounded px-1.5 text-left transition-colors hover:bg-pir-surface-1"
                >
                  <span
                    aria-hidden
                    className={`w-3 text-center text-pir-text-muted transition-transform ${isCollapsed ? "" : "rotate-90"}`}
                  >
                    &gt;
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-pir-text-muted">
                    {program.name}
                  </span>
                  <span className="font-mono text-[10px] text-pir-text-muted">
                    {program.projects.length}
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="ml-3 flex flex-col gap-0.5 border-l border-pir pl-2">
                    {program.projects.map((project) => (
                      <ProjectRow key={project.slug} project={project} activeSlug={activeSlug} />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
