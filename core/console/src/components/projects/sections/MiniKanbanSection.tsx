// v1.0.0 - 2026-04-22 - §3 Mini Kanban readonly (PR #9)
"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import SectionShell from "./SectionShell";
import { listTasks } from "@/lib/api";
import type { TaskResponse, TaskPriority } from "@/lib/types";

const COLS = [
  { key: "pending",     label: "Pending", dot: "bg-pir-warning" },
  { key: "in_progress", label: "Working", dot: "bg-pir-success" },
  { key: "review",      label: "Review",  dot: "bg-[hsl(280_60%_62%)]" },
] as const;

const MAX_PER_COL = 5;

const DELEGATION_BORDER: Record<string, string> = {
  agent:  "hsl(var(--pir-success))",
  hybrid: "hsl(210 80% 55%)",
  human:  "hsl(var(--pir-warning))",
};

const PRIORITY_RANK: Record<TaskPriority, number> = { high: 1, medium: 2, low: 3 };

function PriorityBadge({ value }: { value: TaskPriority | null }) {
  if (value == null) return null;
  const rank = PRIORITY_RANK[value];
  let tone = "bg-pir-text-muted/10 text-pir-text-muted";
  if (rank === 1) tone = "bg-pir-error/15 text-pir-error";
  else if (rank === 2) tone = "bg-pir-warning/15 text-pir-warning";
  return (
    <span
      className={`${tone} font-mono font-bold`}
      style={{
        padding: "2px 5px",
        borderRadius: 2,
        fontSize: 9,
        letterSpacing: "0.04em",
      }}
    >
      P{rank}
    </span>
  );
}

function IceBadge({ score }: { score: number | null | undefined }) {
  if (score == null) return null;
  return (
    <span
      className="bg-pir-accent/15 text-pir-accent font-mono font-bold"
      style={{
        padding: "2px 5px",
        borderRadius: 2,
        fontSize: 9,
        letterSpacing: "0.04em",
      }}
    >
      {Math.round(score)}
    </span>
  );
}

function DelegationBadge({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return (
    <span
      className="bg-pir-success/15 text-pir-success font-mono"
      style={{
        padding: "2px 5px",
        borderRadius: 2,
        fontSize: 9,
        letterSpacing: "0.04em",
      }}
    >
      {value}
    </span>
  );
}

function MiniKanbanSection({ slug }: { slug: string }) {
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const ctrl = new AbortController();
    listTasks(
      { project: slug, status: "pending,approved,in_progress,review", limit: 200 },
      { signal: ctrl.signal }
    )
      .then((res) => setTasks(res))
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [slug]);

  const grouped: Record<string, TaskResponse[]> = {
    pending: [],
    in_progress: [],
    review: [],
  };
  for (const task of tasks) {
    if (task.status === "pending" || task.status === "approved") {
      grouped.pending.push(task);
    } else if (task.status === "in_progress") {
      grouped.in_progress.push(task);
    } else if (task.status === "review") {
      grouped.review.push(task);
    }
  }

  const totalOpen = tasks.length;

  return (
    <SectionShell
      anchorId="kanban"
      eyebrow={totalOpen > 0 ? `Tasks · ${totalOpen} open` : "Tasks"}
      title={`Mini Kanban · ${slug}`}
      action={
        <Link
          href={`/triage/?project=${encodeURIComponent(slug)}`}
          className="text-pir-text-tertiary hover:text-pir-accent transition-colors"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            padding: "4px 8px",
            border: "1px solid hsl(var(--pir-border, 0 0% 0% / 0.12))",
            borderRadius: 2,
          }}
        >
          ↗ Open in Triage
        </Link>
      }
    >
      {loading ? (
        <div className="text-pir-text-tertiary text-sm px-2 py-4">Loading tasks…</div>
      ) : (
        <div className="grid grid-cols-3 gap-2.5">
          {COLS.map((col) => {
            const items = grouped[col.key] || [];
            const visible = items.slice(0, MAX_PER_COL);
            const more = items.length - visible.length;
            return (
              <div
                key={col.key}
                className="bg-pir-surface-0 flex flex-col gap-1.5"
                style={{ borderRadius: 4, padding: 10 }}
              >
                <div className="flex items-center gap-1.5 pb-1.5">
                  <span className={`w-1.5 h-1.5 rounded-full ${col.dot}`} />
                  <span
                    className="text-pir-text-tertiary uppercase"
                    style={{
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 10,
                      fontWeight: 600,
                      letterSpacing: "0.18em",
                    }}
                  >
                    {col.label}
                  </span>
                  <span className="ml-auto text-pir-text-muted font-mono text-[10px]">
                    {items.length}
                  </span>
                </div>
                {visible.length === 0 ? (
                  <div className="text-pir-text-muted text-[11px] px-1 py-2">—</div>
                ) : (
                  visible.map((t) => {
                    const borderColor = DELEGATION_BORDER[t.delegation ?? ""] || "var(--pir-text-muted)";
                    return (
                      <Link
                        key={t.id}
                        href={`/triage/?task=${encodeURIComponent(t.id)}`}
                        className="bg-pir-surface-1 border border-pir block transition-colors hover:border-pir-accent/40"
                        style={{
                          borderRadius: 2,
                          padding: "8px 10px",
                          borderLeft: `2px solid ${borderColor}`,
                          textDecoration: "none",
                          color: "inherit",
                        }}
                      >
                        <div
                          className="text-pir-text-primary line-clamp-2"
                          style={{
                            fontFamily: "var(--pir-font-sans, system-ui)",
                            fontSize: 12,
                            lineHeight: 1.3,
                            fontWeight: 500,
                          }}
                        >
                          {t.title}
                        </div>
                        <div className="flex flex-wrap gap-1 mt-1.5">
                          <IceBadge score={t.ice_score} />
                          <DelegationBadge value={t.delegation} />
                          <PriorityBadge value={t.priority} />
                        </div>
                      </Link>
                    );
                  })
                )}
                {more > 0 && (
                  <Link
                    href={`/triage/?project=${encodeURIComponent(slug)}&status=${col.key}`}
                    className="text-pir-accent hover:text-pir-text-primary transition-colors"
                    style={{
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 10,
                      fontWeight: 500,
                      letterSpacing: "0.12em",
                      textTransform: "uppercase",
                      padding: "6px 4px",
                      textDecoration: "none",
                    }}
                  >
                    + {more} more · filter → Triage
                  </Link>
                )}
              </div>
            );
          })}
        </div>
      )}
    </SectionShell>
  );
}

export default MiniKanbanSection;
