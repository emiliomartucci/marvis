// v1.0.0 - 2026-04-22 - §4 Git recent activity (PR #9)
"use client";

import { useEffect, useState } from "react";
import SectionShell from "./SectionShell";
import { getProjectGitGraph } from "@/lib/api";
import type { GitGraphCommit } from "@/lib/types";

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  if (Number.isNaN(then)) return dateStr;
  const mins = Math.floor((now - then) / 60000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function splitScope(msg: string): { scope: string; body: string } {
  // Parse conventional-commit "type(scope)!:" prefix without a backtracking regex.
  const colonIdx = msg.indexOf(":");
  if (colonIdx <= 0 || colonIdx > 40) return { scope: "", body: msg };
  const head = msg.slice(0, colonIdx);
  const rest = msg.slice(colonIdx + 1).trimStart();
  if (!rest) return { scope: "", body: msg };
  // head must match /[a-z]+( \([^)]+\) )? !?/ roughly — cheap char-level check
  let i = 0;
  while (i < head.length && /[a-zA-Z]/.test(head[i])) i++;
  if (i === 0) return { scope: "", body: msg };
  if (i < head.length && head[i] === "(") {
    const close = head.indexOf(")", i + 1);
    if (close === -1) return { scope: "", body: msg };
    i = close + 1;
  }
  if (i < head.length && head[i] === "!") i++;
  if (i !== head.length) return { scope: "", body: msg };
  return { scope: `${head}:`, body: rest };
}

function GitRecentSection({
  slug,
  onOpenFull,
  supported,
}: {
  slug: string;
  onOpenFull: () => void;
  supported: boolean;
}) {
  const [commits, setCommits] = useState<GitGraphCommit[] | null>(null);
  const [loading, setLoading] = useState(supported);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!supported) {
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    getProjectGitGraph(slug, 10, 0, false, { signal: ctrl.signal })
      .then((res) => setCommits(res.commits))
      .catch((err) => {
        if (err?.name !== "AbortError") setError(err?.message || "Git load failed");
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [slug, supported]);

  if (!supported) return null;

  const topFive = (commits ?? []).slice(0, 5);
  const total = commits?.length ?? 0;

  return (
    <SectionShell
      anchorId="git"
      eyebrow={commits && commits.length > 0 ? `Git · ${total} commits recent` : "Git"}
      title="Recent activity"
      action={
        <button
          type="button"
          onClick={onOpenFull}
          className="text-pir-text-tertiary hover:text-pir-accent transition-colors bg-transparent border border-pir cursor-pointer"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            padding: "4px 8px",
            borderRadius: 2,
          }}
        >
          ↗ Full graph
        </button>
      }
    >
      {loading && <div className="text-pir-text-tertiary text-sm px-2 py-4">Loading commits…</div>}
      {!loading && error && (
        <div className="text-pir-text-tertiary text-sm px-2 py-4">
          Git non disponibile: <span className="font-mono">{error}</span>
        </div>
      )}
      {!loading && !error && (
        <div className="grid gap-4.5 items-start" style={{ gridTemplateColumns: "340px 1fr" }}>
          <div
            className="bg-pir-surface-0 border border-pir"
            style={{ borderRadius: 4, padding: 12 }}
          >
            <MiniGraph commits={topFive} />
          </div>
          <div className="flex flex-col gap-1">
            {topFive.length === 0 ? (
              <div className="text-pir-text-tertiary text-sm px-2 py-4">
                Nessun commit disponibile.
              </div>
            ) : (
              topFive.map((c) => {
                const { scope, body } = splitScope(c.message);
                return (
                  <button
                    type="button"
                    key={c.hash}
                    onClick={onOpenFull}
                    className="hover:bg-pir-surface-1/60 transition-colors grid items-baseline text-left cursor-pointer bg-transparent border-0 border-b border-pir"
                    style={{
                      gridTemplateColumns: "60px 1fr 60px",
                      gap: 10,
                      padding: "6px 10px",
                      fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                      fontSize: 11,
                      lineHeight: 1.3,
                    }}
                  >
                    <span className="text-pir-accent font-semibold">{c.hash_short}</span>
                    <span className="text-pir-text-primary truncate">
                      {scope && <span className="text-pir-text-tertiary font-normal">{scope} </span>}
                      {body}
                    </span>
                    <span className="text-pir-text-muted text-right tabular-nums" style={{ fontSize: 10 }}>
                      {(c.author || "").split(" ")[0].toLowerCase() || "—"} · {timeAgo(c.date)}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </SectionShell>
  );
}

function MiniGraph({ commits }: { commits: GitGraphCommit[] }) {
  if (commits.length === 0) {
    return (
      <div className="text-pir-text-muted text-center text-xs py-6">—</div>
    );
  }
  const W = 310;
  const rowH = 28;
  const H = Math.max(commits.length * rowH, 60);
  const laneX = 30;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} aria-hidden>
      <line x1={laneX} y1={14} x2={laneX} y2={H - 14} stroke="hsl(var(--pir-text-muted))" strokeWidth={1} opacity={0.5} />
      {commits.map((c, i) => {
        const y = 14 + i * rowH;
        const { scope, body } = splitScope(c.message);
        return (
          <g key={c.hash}>
            <circle cx={laneX} cy={y} r={4} fill="hsl(var(--pir-accent))" opacity={0.85} />
            <text
              x={60}
              y={y + 4}
              fill="var(--pir-text-secondary)"
              style={{
                fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                fontSize: 10,
              }}
            >
              {scope ? `${scope.replace(":", "")} · ` : ""}
              {body.length > 28 ? body.slice(0, 28) + "…" : body}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export default GitRecentSection;
