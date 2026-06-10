// v1.0.0 - 2026-04-22 - §1 Context card with preview + expand (PR #9)
"use client";

import { useState } from "react";
import SectionShell from "./SectionShell";
import SafeMarkdown from "@/components/projects/SafeMarkdown";
import type { ProjectDetail } from "@/lib/types";

const PREVIEW_CHARS = 420;

function shorten(md: string | null | undefined, chars: number): string {
  if (!md) return "";
  const trimmed = md.trim();
  if (trimmed.length <= chars) return trimmed;
  const slice = trimmed.slice(0, chars);
  const lastBreak = slice.lastIndexOf("\n\n");
  if (lastBreak > chars / 2) return slice.slice(0, lastBreak);
  return slice + "…";
}

function ContextSection({ project }: { project: ProjectDetail }) {
  const [expanded, setExpanded] = useState(false);
  const ctx = project.context_md || "";
  const preview = shorten(ctx, PREVIEW_CHARS);
  const hasMore = ctx.length > preview.length;
  const title = project.name || project.slug;

  return (
    <SectionShell
      anchorId="context"
      eyebrow="Context · from context.md"
      title={title}
      action={
        project.description ? (
          <span className="text-[10px] font-mono uppercase tracking-[0.12em] text-pir-text-tertiary">
            {project.description.slice(0, 80)}
          </span>
        ) : null
      }
    >
      <div
        className="bg-pir-surface-0 border border-pir"
        style={{ borderRadius: 4, padding: "14px 16px" }}
      >
        {ctx ? (
          <>
            <div
              className="text-pir-text-secondary"
              style={{
                fontFamily: "var(--pir-font-sans, system-ui)",
                fontSize: 13,
                lineHeight: 1.55,
                maxWidth: 820,
              }}
            >
              <SafeMarkdown content={expanded ? ctx : preview} />
            </div>
            {hasMore && (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="mt-3 text-pir-accent hover:text-pir-accent/80 transition-colors cursor-pointer bg-transparent border-0 p-0"
                style={{
                  fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                  fontWeight: 500,
                  fontSize: 10,
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                }}
              >
                {expanded ? "↑ Collapse" : "↓ Expand full context.md"}
              </button>
            )}
          </>
        ) : (
          <div className="text-pir-text-tertiary text-sm">
            No context.md yet. Crealo in <code className="font-mono">{project.metadata_path}/context.md</code>.
          </div>
        )}
      </div>
    </SectionShell>
  );
}

export default ContextSection;
