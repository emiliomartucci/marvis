"use client";

import React, { useEffect, useState } from "react";
import { TagList } from "@/components/ui/TagChip";
import CommentThread from "./CommentThread";
import {
  getMe,
  getStatusUpdates,
  createStatusUpdate,
  getProjectHandoffs,
  getProjectGitGraph,
  getProjectCosts,
  listTasks,
  getProjectRaci,
} from "@/lib/api";
import type {
  ConversationCost,
  ProjectDetail,
  ProjectLifecycle,
  ProjectStatus,
  StatusUpdateResponse,
  HandoffEntry,
  GitGraphCommit,
  RaciEntry,
  RaciRole,
} from "@/lib/types";
import {
  computeLayout,
  bezierPath,
  laneToX,
  rowToY,
  NODE_RADIUS,
  GRAPH_PADDING,
  LANE_SPACING,
} from "@/lib/gitGraphLayout";

const STATUS_OPTIONS: ProjectStatus[] = [
  "active",
  "paused",
  "blocked",
  "completed",
  "not_started",
];

const STATUS_BADGE: Record<ProjectStatus, string> = {
  active: "bg-pir-success/20 text-pir-success",
  paused: "bg-pir-warning/20 text-pir-warning",
  blocked: "bg-pir-error/20 text-pir-error",
  completed: "bg-pir-text-muted/20 text-pir-text-muted",
  not_started: "bg-pir-text-muted/10 text-pir-text-muted",
};

const LIFECYCLE_BADGE: Record<ProjectLifecycle, string> = {
  active: "bg-pir-success/20 text-pir-success",
  planning: "bg-pir-accent/20 text-pir-accent",
  idea: "bg-pir-text-muted/10 text-pir-text-muted",
  maintenance: "bg-pir-warning/20 text-pir-warning",
  archived: "bg-pir-text-muted/10 text-pir-text-muted line-through",
};

function renderInlineMarkdown(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={i} className="text-pir-text font-semibold">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={i} className="text-pir-blue/80 font-mono text-[10px] bg-pir-blue/10 px-0.5 rounded">{part.slice(1, -1)}</code>;
    }
    return part;
  });
}

function parseCommitType(message: string): { type: string | null; body: string } {
  const match = message.match(/^(\w+)(?:\([^)]+\))?!?:\s+(.+)/);
  if (!match) return { type: null, body: message };
  return { type: match[1], body: match[2] };
}

function parsePrNumber(message: string): string | null {
  const match = message.match(/\(#(\d+)\)/);
  return match ? match[1] : null;
}

const COMMIT_TYPE_COLORS: Record<string, string> = {
  feat: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  fix: "bg-rose-500/15 text-rose-700 dark:text-rose-400",
  chore: "bg-pir-text-muted/15 text-pir-text-muted",
  refactor: "bg-blue-500/15 text-blue-700 dark:text-blue-400",
  docs: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  test: "bg-cyan-500/15 text-cyan-700 dark:text-cyan-400",
  style: "bg-pink-500/15 text-pink-700 dark:text-pink-400",
  build: "bg-orange-500/15 text-orange-700 dark:text-orange-400",
  ci: "bg-violet-500/15 text-violet-700 dark:text-violet-400",
  perf: "bg-teal-500/15 text-teal-700 dark:text-teal-400",
};

function timeAgo(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diffMs = now - then;
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays}d ago`;
}

/* ── Mini Git Graph for Overview ── */

const MINI_ROW_H = 28;
const MINI_LANE_W = 18;
const MINI_R = 3;
const MINI_PAD = 10;

function miniLaneX(lane: number) {
  return MINI_PAD + lane * MINI_LANE_W + MINI_LANE_W / 2;
}
function miniRowY(row: number) {
  return MINI_ROW_H / 2 + row * MINI_ROW_H;
}

function MiniGitGraph({ commits }: { commits: GitGraphCommit[] }) {
  const layout = React.useMemo(() => computeLayout(commits), [commits]);
  const svgW = MINI_PAD * 2 + (layout.maxLane + 1) * MINI_LANE_W;
  const svgH = commits.length * MINI_ROW_H;

  return (
    <div className="flex overflow-hidden">
      {/* SVG graph column */}
      <div className="shrink-0" style={{ width: svgW }}>
        <svg width={svgW} height={svgH}>
          {/* edges */}
          {layout.edges.map((e, i) => {
            const x1 = miniLaneX(e.fromLane);
            const y1 = miniRowY(e.fromRow);
            const x2 = miniLaneX(e.toLane);
            const y2 = miniRowY(e.toRow);
            const d =
              x1 === x2
                ? `M ${x1} ${y1} L ${x2} ${y2}`
                : `M ${x1} ${y1} C ${x1} ${y1 + (y2 - y1) * 0.4}, ${x2} ${y1 + (y2 - y1) * 0.6}, ${x2} ${y2}`;
            return (
              <path
                key={i}
                d={d}
                fill="none"
                stroke={e.color}
                strokeWidth={1.5}
                opacity={0.6}
              />
            );
          })}
          {/* nodes */}
          {layout.nodes.map((n) => (
            <circle
              key={n.commit.hash}
              cx={miniLaneX(n.lane)}
              cy={miniRowY(n.row)}
              r={MINI_R}
              fill={n.color}
            />
          ))}
        </svg>
      </div>

      {/* commit info column */}
      <div className="flex-1 min-w-0">
        {layout.nodes.map((n) => {
          const c = n.commit;
          const { type, body } = parseCommitType(c.message);
          const typeColor = type
            ? COMMIT_TYPE_COLORS[type] || "bg-pir-text-muted/15 text-pir-text-muted"
            : null;
          return (
            <div
              key={c.hash}
              className="flex items-center gap-1.5 px-1.5 overflow-hidden"
              style={{ height: MINI_ROW_H }}
            >
              {/* ref badges */}
              {c.refs
                .filter((r) => !r.startsWith("origin/"))
                .slice(0, 1)
                .map((r) => {
                  const label = r
                    .replace("HEAD -> ", "")
                    .replace("tag: ", "");
                  return (
                    <span
                      key={r}
                      className="shrink-0 text-[8px] font-mono font-medium px-1 py-0.5 rounded border truncate max-w-[80px]"
                      style={{
                        color: n.color,
                        borderColor: n.color,
                        backgroundColor: `color-mix(in srgb, ${n.color} 12%, transparent)`,
                      }}
                    >
                      {label}
                    </span>
                  );
                })}
              {type && typeColor && (
                <span className={`shrink-0 text-[8px] px-1 py-0.5 rounded font-medium ${typeColor}`}>
                  {type}
                </span>
              )}
              <span className="text-[11px] text-pir-text-secondary truncate">
                {type ? body : c.message}
              </span>
              <span className="shrink-0 text-[9px] text-pir-text-muted ml-auto">
                {timeAgo(c.date)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface MetricCardProps {
  label: string;
  value: React.ReactNode;
  subtitle?: string;
  accentColor?: string;
}

function MetricCard({ label, value, subtitle, accentColor = "border-l-pir-accent" }: MetricCardProps) {
  return (
    <div className={`bg-pir-surface-1 border border-pir rounded p-3 border-l-2 ${accentColor}`}>
      <div className="text-caption uppercase tracking-wider text-pir-text-muted mb-1">
        {label}
      </div>
      <div className="text-heading text-pir-text-primary">{value}</div>
      {subtitle && (
        <div className="text-caption text-pir-text-muted mt-0.5">{subtitle}</div>
      )}
    </div>
  );
}

export default function OverviewTab({ project }: { project: ProjectDetail }) {
  const [updates, setUpdates] = useState<StatusUpdateResponse[]>([]);
  const [handoffs, setHandoffs] = useState<HandoffEntry[]>([]);
  const [commits, setCommits] = useState<GitGraphCommit[]>([]);
  const [formStatus, setFormStatus] = useState<ProjectStatus>("active");
  const [whatDone, setWhatDone] = useState("");
  const [blockers, setBlockers] = useState("");
  const [nextSteps, setNextSteps] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [currentUser, setCurrentUser] = useState<string>("");
  const [costData, setCostData] = useState<ConversationCost[]>([]);
  const [openTaskCount, setOpenTaskCount] = useState<number | null>(null);
  const [raciEntries, setRaciEntries] = useState<RaciEntry[]>([]);

  useEffect(() => {
    getMe()
      .then((u) => setCurrentUser(u.username))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getStatusUpdates(project.slug, { signal: controller.signal })
      .then(setUpdates)
      .catch(() => {});

    getProjectHandoffs(project.slug, { signal: controller.signal })
      .then(setHandoffs)
      .catch(() => {});

    getProjectGitGraph(project.slug, 10, 0, true, { signal: controller.signal })
      .then((res) => setCommits(res.commits))
      .catch(() => {});

    getProjectCosts(project.slug, {}, { signal: controller.signal })
      .then(setCostData)
      .catch(() => {});

    listTasks(
      { project: project.slug, status: "pending,approved,in_progress", limit: 500 },
      { signal: controller.signal }
    )
      .then((tasks) => setOpenTaskCount(tasks.length))
      .catch(() => {});

    getProjectRaci(project.slug)
      .then(setRaciEntries)
      .catch(() => {});

    return () => controller.abort();
  }, [project.slug]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const res = await createStatusUpdate({
        project: project.slug,
        status: formStatus,
        what_done: whatDone || null,
        blockers: blockers || null,
        next_steps: nextSteps || null,
      });
      setUpdates((prev) => [res, ...prev]);
      setWhatDone("");
      setBlockers("");
      setNextSteps("");
    } catch {
      // ignore
    } finally {
      setSubmitting(false);
    }
  }

  const latestStatus = updates[0]?.status ?? null;
  const latestCommit = commits[0] ?? null;

  return (
    <div className="space-y-4">
      {/* Metric cards row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <MetricCard
          label="Lifecycle"
          accentColor="border-l-pir-success"
          value={
            project.lifecycle ? (
              <span
                className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${LIFECYCLE_BADGE[project.lifecycle]}`}
              >
                {project.lifecycle}
              </span>
            ) : latestStatus ? (
              <span
                className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${STATUS_BADGE[latestStatus]}`}
              >
                {latestStatus}
              </span>
            ) : (
              <span className="text-pir-text-muted text-sm">—</span>
            )
          }
          subtitle={project.language ?? project.config?.language ?? undefined}
        />
        <MetricCard
          label="Tasks"
          accentColor="border-l-pir-warning"
          value={
            openTaskCount === null ? (
              <span className="text-pir-text-muted">—</span>
            ) : (
              <span className="tabular-nums">{openTaskCount}</span>
            )
          }
          subtitle="open tasks"
        />
        <MetricCard
          label="Cost"
          accentColor="border-l-teal-500"
          value={
            costData.length > 0 ? (
              <span className="tabular-nums">
                ${costData.reduce((sum, c) => sum + c.cost_usd, 0).toFixed(2)}
              </span>
            ) : (
              <span className="text-pir-text-muted">—</span>
            )
          }
          subtitle={costData.length > 0 ? `${costData.length} conversations` : undefined}
        />
        <MetricCard
          label="Git"
          accentColor="border-l-pir-purple"
          value={
            latestCommit ? (
              <span className="font-mono text-sm text-pir-purple">{latestCommit.hash_short}</span>
            ) : (
              <span className="text-pir-text-muted">—</span>
            )
          }
          subtitle={latestCommit ? timeAgo(latestCommit.date) : undefined}
        />
      </div>

      {/* RACI chips */}
      {raciEntries.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {raciEntries.map((entry) => {
            const roleColors: Record<RaciRole, string> = {
              responsible: "bg-blue-400/15 text-blue-700 dark:text-blue-400 border-blue-400/30",
              accountable: "bg-amber-400/15 text-amber-700 dark:text-amber-400 border-amber-400/30",
              consulted: "bg-purple-400/15 text-purple-700 dark:text-purple-400 border-purple-400/30",
              informed: "bg-pir-text-muted/10 text-pir-text-muted border-pir/50",
            };
            const roleLabel: Record<RaciRole, string> = {
              responsible: "R",
              accountable: "A",
              consulted: "C",
              informed: "I",
            };
            return (
              <span
                key={`${entry.role}-${entry.user.id}`}
                className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs border ${roleColors[entry.role]}`}
              >
                <span className="font-bold">{roleLabel[entry.role]}:</span>
                {entry.user.display_name}
              </span>
            );
          })}
        </div>
      )}

      {/* Middle panels */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* Recent Handoffs */}
        <div className="bg-pir-surface-1 border border-pir rounded p-3">
          <div className="text-label text-pir-text-primary mb-2 flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-pir-accent shrink-0" />
            Recent Handoffs
          </div>
          {handoffs.length === 0 ? (
            <div className="text-caption text-pir-text-muted">No handoffs yet</div>
          ) : (
            <ul className="space-y-0">
              {handoffs.slice(0, 5).map((h) => (
                <li key={h.filename} className="pb-2 border-b border-pir/40 last:border-0 last:pb-0">
                  <div className="flex items-start gap-2">
                    {h.session != null && (
                      <span className="shrink-0 text-[10px] font-mono font-bold text-pir-accent bg-pir-accent/10 px-1.5 py-0.5 rounded mt-0.5">
                        #{h.session}
                      </span>
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 flex-wrap mb-0.5">
                        <span className="text-[10px] text-pir-text-muted font-mono">{h.date}</span>
                        {h.branch && (
                          <span className="text-[10px] text-pir-purple/70 font-mono truncate max-w-[130px]">{h.branch}</span>
                        )}
                      </div>
                      {h.tags.length > 0 && (
                        <div className="mb-0.5">
                          <TagList tags={h.tags} />
                        </div>
                      )}
                      {h.summary && (
                        <div className="text-[11px] text-pir-text-secondary line-clamp-2 leading-relaxed">{renderInlineMarkdown(h.summary)}</div>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Recent Activity — mini git graph */}
        <div className="bg-pir-surface-1 border border-pir rounded p-3">
          <div className="text-label text-pir-text-primary mb-2 flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-pir-purple shrink-0" />
            Recent Activity
          </div>
          {commits.length === 0 ? (
            <div className="text-caption text-pir-text-muted">No commits yet</div>
          ) : (
            <MiniGitGraph commits={commits.slice(0, 10)} />
          )}
        </div>
      </div>

      {/* Project Info (from project.yaml) */}
      {(project.description || project.phase || project.language || project.deploy || project.scope) && (
        <div className="bg-pir-surface-1 border border-pir rounded p-3 space-y-2">
          <div className="text-label text-pir-text-primary flex items-center gap-2">
            Project Info
            {project.scope && (
              <span className={`text-caption px-1.5 py-0.5 rounded font-mono ${
                project.scope === "work" ? "bg-blue-500/15 text-blue-400" : "bg-pir-text-muted/10 text-pir-text-muted"
              }`}>
                {project.scope}
              </span>
            )}
          </div>
          {project.description && (
            <div className="text-body text-pir-text-secondary">{project.description}</div>
          )}
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            {project.phase && (
              <div className="flex items-center gap-1.5 text-xs">
                <span className="text-caption text-pir-text-muted uppercase tracking-wider">phase</span>
                <span className="text-body text-pir-text-secondary">{project.phase}</span>
              </div>
            )}
            {project.language && (
              <div className="flex items-center gap-1.5 text-xs">
                <span className="text-caption text-pir-text-muted uppercase tracking-wider">lang</span>
                <span className="text-body text-pir-text-secondary font-mono">{project.language}</span>
              </div>
            )}
            {project.deploy?.hosting && (
              <div className="flex items-center gap-1.5 text-xs">
                <span className="text-caption text-pir-text-muted uppercase tracking-wider">hosting</span>
                <span className="text-body text-pir-text-secondary">{project.deploy.hosting}</span>
              </div>
            )}
          </div>
          {project.deploy && (project.deploy.url || project.deploy.api_url) && (
            <div className="flex flex-wrap gap-3">
              {project.deploy.url && (
                <a
                  href={project.deploy.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-caption text-pir-accent hover:text-pir-accent/80 transition-colors font-mono"
                >
                  {project.deploy.url.replace(/^https?:\/\//, "")}
                </a>
              )}
              {project.deploy.api_url && (
                <a
                  href={project.deploy.api_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-caption text-pir-accent/70 hover:text-pir-accent transition-colors font-mono"
                >
                  {project.deploy.api_url.replace(/^https?:\/\//, "")} (API)
                </a>
              )}
            </div>
          )}
        </div>
      )}

      {/* Legacy Config fallback (projects without project.yaml) */}
      {!project.description && !project.phase && Object.keys(project.config).length > 0 && (
        <div className="bg-pir-surface-1 border border-pir rounded p-3">
          <div className="text-label text-pir-text-primary mb-2">Config</div>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {Object.entries(project.config).map(([key, val]) => (
              <div key={key} className="flex items-center gap-1.5 text-xs">
                <span className="text-caption text-pir-text-muted uppercase tracking-wider">
                  {key}
                </span>
                <span className="text-body text-pir-text-secondary">{val}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Status update form */}
      <div className="bg-pir-surface-1 border border-pir rounded p-3">
        <form onSubmit={handleSubmit} className="space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="text-label text-pir-text-primary">Status Update</h3>
            <select
              value={formStatus}
              onChange={(e) => setFormStatus(e.target.value as ProjectStatus)}
              className="bg-pir-surface-0 border border-pir rounded px-2 py-1 text-xs text-pir-text-primary ml-auto"
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            <input
              type="text"
              placeholder="What was done..."
              value={whatDone}
              onChange={(e) => setWhatDone(e.target.value)}
              className="bg-pir-surface-0 border border-pir rounded px-2.5 py-1.5 text-xs text-pir-text-primary placeholder:text-pir-text-muted"
            />
            <input
              type="text"
              placeholder="Blockers..."
              value={blockers}
              onChange={(e) => setBlockers(e.target.value)}
              className="bg-pir-surface-0 border border-pir rounded px-2.5 py-1.5 text-xs text-pir-text-primary placeholder:text-pir-text-muted"
            />
            <input
              type="text"
              placeholder="Next steps..."
              value={nextSteps}
              onChange={(e) => setNextSteps(e.target.value)}
              className="bg-pir-surface-0 border border-pir rounded px-2.5 py-1.5 text-xs text-pir-text-primary placeholder:text-pir-text-muted"
            />
          </div>
          <button
            type="submit"
            disabled={submitting}
            className="px-3 py-1 bg-pir-accent text-white text-xs rounded hover:bg-pir-accent/80 disabled:opacity-50"
          >
            {submitting ? "Saving..." : "Submit"}
          </button>
        </form>
      </div>

      {/* Status history */}
      {updates.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-label text-pir-text-primary">Status History</h3>
          {updates.slice(0, 10).map((u) => (
            <div
              key={u.id}
              className="bg-pir-surface-1 border border-pir rounded p-3 text-xs"
            >
              <div className="flex items-center gap-2 mb-1">
                <span
                  className={`inline-block px-2 py-0.5 rounded font-medium ${STATUS_BADGE[u.status]}`}
                >
                  {u.status}
                </span>
                <span className="text-caption text-pir-text-muted">{u.created_by}</span>
                <span className="text-caption text-pir-text-muted ml-auto">
                  {new Date(u.created_at).toLocaleDateString()}
                </span>
              </div>
              {u.what_done && (
                <div className="text-body text-pir-text-secondary">Done: {u.what_done}</div>
              )}
              {u.blockers && (
                <div className="text-body text-pir-error">Blockers: {u.blockers}</div>
              )}
              {u.next_steps && (
                <div className="text-body text-pir-text-muted">Next: {u.next_steps}</div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Comments */}
      {currentUser && (
        <div>
          <h3 className="text-label text-pir-text-primary mb-3">Comments</h3>
          <CommentThread
            targetType="project"
            targetId={project.slug}
            currentUser={currentUser}
          />
        </div>
      )}
    </div>
  );
}
