// v1.0.0 - 2026-04-22 - Subbar for /projects/detail single-pager v2 (PR #9)
"use client";

import Link from "next/link";
import type { ProjectDetail } from "@/lib/types";

interface ProjectsSubbarProps {
  project: ProjectDetail;
  totalProjects?: number;
  cost7d?: number | null;
}

function fmtDollar(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v < 10) return `$${v.toFixed(2)}`;
  return `$${v.toFixed(0)}`;
}

function ProjectsSubbar({ project, totalProjects, cost7d }: ProjectsSubbarProps) {
  const lifecycle = project.lifecycle || "—";
  const language = project.language || project.config?.language;
  return (
    <div
      className="bg-pir-surface-0 border-b border-pir flex items-center shrink-0"
      style={{ height: 44 }}
    >
      <div
        className="flex items-center justify-between border-r border-pir h-full"
        style={{ width: 240, padding: "0 16px" }}
      >
        <span className="flex items-center gap-1.5 text-pir-text-primary uppercase text-[13px] font-bold tracking-[0.08em]">
          <span
            className="text-pir-text-tertiary font-mono font-semibold"
            style={{ fontSize: 10, letterSpacing: "0.22em" }}
          >
            Projects
          </span>
          {typeof totalProjects === "number" && totalProjects > 0 && (
            <span className="tabular-nums">{totalProjects}</span>
          )}
        </span>
      </div>
      <div className="flex-1 flex items-center gap-3.5" style={{ padding: "0 14px" }}>
        <nav className="flex items-center gap-1.5 text-pir-text-primary font-bold text-[13px]">
          <Link
            href="/projects/"
            className="text-pir-text-tertiary font-medium hover:text-pir-text-primary transition-colors"
            style={{ textDecoration: "none" }}
          >
            Projects
          </Link>
          <span className="text-pir-text-muted px-0.5" aria-hidden>·</span>
          <span className="font-mono">{project.slug}</span>
        </nav>
        <div className="flex items-center gap-1.5">
          <Pill>
            <span
              className="inline-block rounded-full bg-pir-success"
              style={{ width: 6, height: 6 }}
              aria-hidden
            />
            {lifecycle}
          </Pill>
          {language && (
            <Pill tone="accent">
              {language}
            </Pill>
          )}
          {cost7d != null && (
            <Pill tone="strong">{fmtDollar(cost7d)} · 7d</Pill>
          )}
        </div>
        <Link
          href={`/graph?id=${encodeURIComponent("project:artifact:" + project.slug)}`}
          className="ml-auto text-pir-text-tertiary hover:text-pir-accent hover:border-pir-accent/50 transition-colors border border-pir"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            padding: "4px 8px",
            borderRadius: 2,
            textDecoration: "none",
          }}
        >
          ↗ Open in Graph
        </Link>
      </div>
    </div>
  );
}

function Pill({ children, tone }: { children: React.ReactNode; tone?: "accent" | "strong" }) {
  const base =
    "inline-flex items-center gap-1.5 border rounded-[2px] px-2 py-[3px] font-mono text-[10px] font-medium";
  if (tone === "accent") {
    return (
      <span
        className={`${base} text-pir-accent`}
        style={{
          borderColor: "hsl(var(--pir-accent) / 0.3)",
          background: "hsl(var(--pir-accent) / 0.08)",
        }}
      >
        {children}
      </span>
    );
  }
  if (tone === "strong") {
    return (
      <span className={`${base} text-pir-text-primary border-pir font-bold`}>
        {children}
      </span>
    );
  }
  return (
    <span className={`${base} text-pir-text-secondary border-pir`}>
      {children}
    </span>
  );
}

export default ProjectsSubbar;
