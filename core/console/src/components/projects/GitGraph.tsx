"use client";

import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import {
  getProjectGitGraph,
  getProjectGitDiff,
  getGitCommitDetail,
  projectGitPush,
  projectGitPull,
} from "@/lib/api";
import type {
  GitGraphCommit,
  GitRef,
  GitCommitDetail,
  GraphNode,
  GraphEdge,
} from "@/lib/types";
import {
  computeLayout,
  bezierPath,
  laneToX,
  rowToY,
  COMMIT_SPACING,
  LANE_SPACING,
  NODE_RADIUS,
  GRAPH_PADDING,
} from "@/lib/gitGraphLayout";

const PAGE_SIZE = 50;

function timeAgo(dateStr: string): string {
  const seconds = Math.floor(
    (Date.now() - new Date(dateStr).getTime()) / 1000
  );
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
}

// Parse conventional commit type from message
function parseCommitType(message: string): {
  type: string;
  scope: string | null;
  rest: string;
} {
  const match = message.match(/^(\w+)(?:\(([^)]*)\))?[!]?:\s*(.*)/);
  if (match) {
    return { type: match[1], scope: match[2] || null, rest: match[3] };
  }
  return { type: "", scope: null, rest: message };
}

// Commit type badge colors
const COMMIT_TYPE_STYLES: Record<
  string,
  { bg: string; text: string; border: string }
> = {
  feat: {
    bg: "rgba(64, 196, 99, 0.12)",
    text: "hsl(140 60% 50%)",
    border: "rgba(64, 196, 99, 0.25)",
  },
  fix: {
    bg: "rgba(227, 72, 80, 0.12)",
    text: "hsl(0 80% 62%)",
    border: "rgba(227, 72, 80, 0.25)",
  },
  refactor: {
    bg: "rgba(163, 102, 230, 0.12)",
    text: "hsl(270 60% 65%)",
    border: "rgba(163, 102, 230, 0.25)",
  },
  docs: {
    bg: "rgba(77, 196, 196, 0.12)",
    text: "hsl(180 60% 50%)",
    border: "rgba(77, 196, 196, 0.25)",
  },
  chore: {
    bg: "rgba(255, 255, 255, 0.04)",
    text: "rgba(255, 255, 255, 0.44)",
    border: "rgba(255, 255, 255, 0.08)",
  },
  style: {
    bg: "rgba(255, 170, 51, 0.12)",
    text: "hsl(40 80% 50%)",
    border: "rgba(255, 170, 51, 0.25)",
  },
  perf: {
    bg: "rgba(255, 170, 51, 0.12)",
    text: "hsl(40 80% 50%)",
    border: "rgba(255, 170, 51, 0.25)",
  },
  test: {
    bg: "rgba(163, 102, 230, 0.12)",
    text: "hsl(270 60% 65%)",
    border: "rgba(163, 102, 230, 0.25)",
  },
  ci: {
    bg: "rgba(255, 255, 255, 0.04)",
    text: "rgba(255, 255, 255, 0.44)",
    border: "rgba(255, 255, 255, 0.08)",
  },
};

function getCommitTypeStyle(type: string) {
  return (
    COMMIT_TYPE_STYLES[type] || {
      bg: "rgba(255, 255, 255, 0.04)",
      text: "rgba(255, 255, 255, 0.44)",
      border: "rgba(255, 255, 255, 0.08)",
    }
  );
}

export default function GitGraph({ slug }: { slug: string }) {
  const [commits, setCommits] = useState<GitGraphCommit[]>([]);
  const [refs, setRefs] = useState<GitRef[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [allBranches, setAllBranches] = useState(true);

  // Git operations state
  const [pushing, setPushing] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [diff, setDiff] = useState("");

  // Detail panel
  const [selectedHash, setSelectedHash] = useState<string | null>(null);
  const [detail, setDetail] = useState<GitCommitDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // Hover + tooltip
  const [hoveredHash, setHoveredHash] = useState<string | null>(null);
  const [tooltipPos, setTooltipPos] = useState<{ x: number; y: number } | null>(null);
  const [copiedHash, setCopiedHash] = useState(false);
  const tooltipTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchData = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      getProjectGitGraph(slug, PAGE_SIZE, 0, allBranches, { signal })
        .then((res) => {
          setCommits(res.commits);
          setRefs(res.refs);
          setHasMore(res.has_more);
        })
        .catch(() => {})
        .finally(() => {
          if (!signal?.aborted) setLoading(false);
        });
    },
    [slug, allBranches]
  );

  useEffect(() => {
    const controller = new AbortController();
    fetchData(controller.signal);
    return () => controller.abort();
  }, [fetchData]);

  async function loadMore() {
    setLoadingMore(true);
    try {
      const res = await getProjectGitGraph(
        slug,
        PAGE_SIZE,
        commits.length,
        allBranches
      );
      setCommits((prev) => [...prev, ...res.commits]);
      setHasMore(res.has_more);
    } catch {
      // ignore
    } finally {
      setLoadingMore(false);
    }
  }

  async function handleSelectCommit(hash: string) {
    if (selectedHash === hash) {
      setSelectedHash(null);
      setDetail(null);
      return;
    }
    setSelectedHash(hash);
    setLoadingDetail(true);
    try {
      const d = await getGitCommitDetail(slug, hash);
      setDetail(d);
    } catch {
      setDetail(null);
    } finally {
      setLoadingDetail(false);
    }
  }

  async function handleLoadDiff() {
    setShowDiff(true);
    try {
      const res = await getProjectGitDiff(slug);
      setDiff(res.diff);
    } catch {
      setDiff("Failed to load diff");
    }
  }

  async function handlePush() {
    if (!confirm("Push to remote?")) return;
    setPushing(true);
    setMessage(null);
    try {
      const res = await projectGitPush(slug);
      setMessage(
        res.success ? "Push successful" : `Push failed: ${res.error}`
      );
    } catch (err) {
      setMessage(
        `Error: ${err instanceof Error ? err.message : "unknown"}`
      );
    } finally {
      setPushing(false);
    }
  }

  async function handlePull() {
    setPulling(true);
    setMessage(null);
    try {
      const res = await projectGitPull(slug);
      setMessage(
        res.success ? "Pull successful" : `Pull failed: ${res.error}`
      );
      if (res.success) fetchData();
    } catch (err) {
      setMessage(
        `Error: ${err instanceof Error ? err.message : "unknown"}`
      );
    } finally {
      setPulling(false);
    }
  }

  function handleCopyHash() {
    if (!detail) return;
    navigator.clipboard.writeText(detail.hash).then(() => {
      setCopiedHash(true);
      setTimeout(() => setCopiedHash(false), 1500);
    });
  }

  // Layout computation
  const { nodes, edges, maxLane } = useMemo(
    () => computeLayout(commits),
    [commits]
  );

  const graphWidth = GRAPH_PADDING * 2 + (maxLane + 1) * LANE_SPACING;
  const graphHeight = commits.length * COMMIT_SPACING;

  // Find current branch from refs
  const currentBranch = useMemo(() => {
    const headRef = commits[0]?.refs.find((r) => r.startsWith("HEAD -> "));
    return headRef ? headRef.replace("HEAD -> ", "") : null;
  }, [commits]);

  // Commit being hovered (for tooltip)
  const hoveredCommit = useMemo(
    () => (hoveredHash ? commits.find((c) => c.hash === hoveredHash) ?? null : null),
    [hoveredHash, commits]
  );

  if (loading) {
    return (
      <div className="text-pir-text-muted text-body p-4">
        Loading git graph...
      </div>
    );
  }

  if (commits.length === 0) {
    return (
      <div className="text-pir-text-muted text-body p-4">
        No commits found.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Header bar */}
      <div className="flex flex-wrap items-center gap-3 px-3 py-2 bg-pir-surface-0 border border-pir rounded">
        <div className="flex items-center gap-2">
          <svg
            width="14"
            height="14"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            className="text-pir-text-tertiary"
          >
            <path d="M8 2v12" />
            <circle cx="8" cy="4" r="2" fill="currentColor" stroke="none" />
            <circle cx="8" cy="12" r="2" fill="currentColor" stroke="none" />
          </svg>
          <span className="text-caption text-pir-text-tertiary">Branch</span>
        </div>
        <span className="text-body font-mono text-pir-accent font-medium">
          {currentBranch || "detached"}
        </span>

        {/* Toggle all/current */}
        <button
          onClick={() => setAllBranches((v) => !v)}
          title={allBranches ? "All branches" : "Current branch only"}
          className={`flex items-center gap-1.5 px-2 py-1 rounded text-[10px] font-medium uppercase tracking-wider transition-colors duration-100 ${
            allBranches
              ? "text-pir-accent bg-pir-accent/10 border border-pir-accent/20"
              : "text-pir-text-muted hover:text-pir-text-secondary border border-transparent"
          }`}
        >
          {allBranches ? (
            <svg
              width="12"
              height="12"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            >
              <path d="M8 2v6" />
              <path d="M8 8L4 12" />
              <path d="M8 8l4 4" />
              <circle cx="8" cy="2" r="1.5" fill="currentColor" stroke="none" />
              <circle cx="4" cy="12" r="1.5" fill="currentColor" stroke="none" />
              <circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none" />
            </svg>
          ) : (
            <svg
              width="12"
              height="12"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            >
              <path d="M8 2v12" />
              <circle cx="8" cy="2" r="1.5" fill="currentColor" stroke="none" />
              <circle cx="8" cy="14" r="1.5" fill="currentColor" stroke="none" />
            </svg>
          )}
          {allBranches ? "All" : "Current"}
        </button>

        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={handlePull}
            disabled={pulling || pushing}
            className="flex items-center gap-1 px-2.5 py-1 text-caption font-medium bg-pir-surface-1 border border-pir rounded hover:border-pir-strong hover:bg-pir-surface-2 disabled:opacity-40 transition-colors duration-100"
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M8 3v10M4 9l4 4 4-4" />
            </svg>
            {pulling ? "..." : "Pull"}
          </button>
          <button
            onClick={handlePush}
            disabled={pushing || pulling}
            className="flex items-center gap-1 px-2.5 py-1 text-caption font-medium bg-pir-accent/15 text-pir-accent border border-pir-accent/30 rounded hover:bg-pir-accent/25 disabled:opacity-40 transition-colors duration-100"
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M8 13V3M4 7l4-4 4 4" />
            </svg>
            {pushing ? "..." : "Push"}
          </button>
          {!showDiff && (
            <button
              onClick={handleLoadDiff}
              className="flex items-center gap-1 px-2.5 py-1 text-caption font-medium bg-pir-surface-1 border border-pir rounded hover:border-pir-strong hover:bg-pir-surface-2 transition-colors duration-100"
            >
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4 4h8M4 8h5M4 12h8" />
              </svg>
              Diff
            </button>
          )}
        </div>
      </div>

      {message && (
        <div
          className={`text-caption px-3 py-2 rounded border ${
            message.includes("failed") || message.includes("Error")
              ? "bg-pir-error/10 text-pir-error border-pir-error/20"
              : "bg-pir-success/10 text-pir-success border-pir-success/20"
          }`}
        >
          {message}
        </div>
      )}

      {showDiff && diff && (
        <div className="bg-pir-surface-0 border border-pir rounded p-3 max-h-64 overflow-y-auto">
          <pre className="text-caption font-mono text-pir-text-secondary whitespace-pre-wrap">
            {diff}
          </pre>
        </div>
      )}

      {/* Graph + commit list */}
      <div className="overflow-x-auto border border-pir rounded">
        <div className="flex" style={{ minWidth: graphWidth + 400 }}>
          {/* SVG Graph column */}
          <div className="shrink-0" style={{ width: graphWidth }}>
            <svg
              width={graphWidth}
              height={graphHeight}
              className="block"
            >
              <defs>
                {/* Glow filters for nodes */}
                {nodes.slice(0, 8).map((node, i) => {
                  const uniqueColors = [...new Set(nodes.map((n) => n.color))];
                  const colorIdx = uniqueColors.indexOf(node.color);
                  if (colorIdx !== i) return null;
                  return (
                    <filter
                      key={`glow-${i}`}
                      id={`glow-${node.color.replace(/[^a-z0-9]/gi, "")}`}
                      x="-50%"
                      y="-50%"
                      width="200%"
                      height="200%"
                    >
                      <feGaussianBlur stdDeviation="2" result="blur" />
                      <feMerge>
                        <feMergeNode in="blur" />
                        <feMergeNode in="SourceGraphic" />
                      </feMerge>
                    </filter>
                  );
                })}
              </defs>

              {/* Row backgrounds for alternating stripes */}
              {nodes.map((node, i) => (
                <rect
                  key={`bg-${i}`}
                  x={0}
                  y={i * COMMIT_SPACING}
                  width={graphWidth}
                  height={COMMIT_SPACING}
                  fill={
                    selectedHash === node.commit.hash
                      ? "rgba(255, 255, 255, 0.04)"
                      : hoveredHash === node.commit.hash
                      ? "rgba(255, 255, 255, 0.03)"
                      : i % 2 === 0
                      ? "transparent"
                      : "rgba(255, 255, 255, 0.015)"
                  }
                  className="transition-colors duration-100"
                />
              ))}

              {/* Edges */}
              {edges.map((edge, i) => {
                const x1 = laneToX(edge.fromLane);
                const y1 = rowToY(edge.fromRow);
                const x2 = laneToX(edge.toLane);
                const y2 = rowToY(edge.toRow);
                const isHighlighted =
                  hoveredHash === edge.fromHash ||
                  hoveredHash === edge.toHash;
                return (
                  <path
                    key={i}
                    d={bezierPath(x1, y1, x2, y2)}
                    stroke={edge.color}
                    strokeWidth={isHighlighted ? 2.5 : 1.5}
                    fill="none"
                    opacity={
                      hoveredHash && !isHighlighted ? 0.2 : 0.8
                    }
                    className="transition-opacity duration-100"
                  />
                );
              })}

              {/* Nodes */}
              {nodes.map((node) => {
                const cx = laneToX(node.lane);
                const cy = rowToY(node.row);
                const isSelected = selectedHash === node.commit.hash;
                const isHovered = hoveredHash === node.commit.hash;
                const isMerge = node.commit.parents.length > 1;
                const glowId = `glow-${node.color.replace(/[^a-z0-9]/gi, "")}`;
                return (
                  <g
                    key={node.commit.hash}
                    className="cursor-pointer"
                    onMouseEnter={(e) => {
                      if (tooltipTimeoutRef.current) clearTimeout(tooltipTimeoutRef.current);
                      setHoveredHash(node.commit.hash);
                      setTooltipPos({ x: e.clientX, y: e.clientY });
                    }}
                    onMouseMove={(e) => {
                      setTooltipPos({ x: e.clientX, y: e.clientY });
                    }}
                    onMouseLeave={() => {
                      tooltipTimeoutRef.current = setTimeout(() => {
                        setHoveredHash(null);
                        setTooltipPos(null);
                      }, 80);
                    }}
                    onClick={() =>
                      handleSelectCommit(node.commit.hash)
                    }
                  >
                    {/* Outer ring for selected/hovered */}
                    {(isSelected || isHovered) && (
                      <circle
                        cx={cx}
                        cy={cy}
                        r={NODE_RADIUS + 3}
                        fill="none"
                        stroke={node.color}
                        strokeWidth={1}
                        opacity={isSelected ? 0.6 : 0.3}
                        filter={isSelected ? `url(#${glowId})` : undefined}
                      />
                    )}
                    {/* Main node */}
                    <circle
                      cx={cx}
                      cy={cy}
                      r={NODE_RADIUS}
                      fill={node.color}
                      opacity={
                        hoveredHash && !isHovered && !isSelected ? 0.4 : 1
                      }
                      className="transition-opacity duration-100"
                    />
                    {/* Inner bright dot */}
                    <circle
                      cx={cx}
                      cy={cy}
                      r={NODE_RADIUS * 0.4}
                      fill="rgba(255, 255, 255, 0.5)"
                      opacity={
                        hoveredHash && !isHovered && !isSelected ? 0.2 : 0.6
                      }
                    />
                    {/* Merge indicator: diamond shape overlay */}
                    {isMerge && (
                      <rect
                        x={cx - 2}
                        y={cy - 2}
                        width={4}
                        height={4}
                        fill="white"
                        opacity={0.7}
                        transform={`rotate(45 ${cx} ${cy})`}
                      />
                    )}
                  </g>
                );
              })}
            </svg>
          </div>

          {/* Vertical separator */}
          <div
            className="shrink-0 w-px"
            style={{ background: "rgba(255, 255, 255, 0.06)" }}
          />

          {/* Commit info column */}
          <div className="flex-1 min-w-0">
            {nodes.map((node, i) => {
              const commit = node.commit;
              const isSelected = selectedHash === commit.hash;
              const isHovered = hoveredHash === commit.hash;
              const { type, scope, rest } = parseCommitType(commit.message);
              const typeStyle = getCommitTypeStyle(type);
              return (
                <div
                  key={commit.hash}
                  className={`flex items-center gap-2 px-3 cursor-pointer transition-colors duration-100 border-b border-transparent ${
                    isSelected
                      ? "bg-pir-surface-2"
                      : isHovered
                      ? "bg-pir-surface-1"
                      : i % 2 === 0
                      ? "bg-transparent"
                      : ""
                  }`}
                  style={{
                    height: COMMIT_SPACING,
                    backgroundColor: isSelected
                      ? undefined
                      : isHovered
                      ? undefined
                      : i % 2 !== 0
                      ? "rgba(255, 255, 255, 0.015)"
                      : undefined,
                  }}
                  onMouseEnter={() => setHoveredHash(commit.hash)}
                  onMouseLeave={() => setHoveredHash(null)}
                  onClick={() => handleSelectCommit(commit.hash)}
                >
                  {/* Ref badges */}
                  {commit.refs
                    .filter((r) => !r.startsWith("origin/"))
                    .map((ref) => {
                      const isHead = ref.startsWith("HEAD -> ");
                      const label = isHead ? ref.replace("HEAD -> ", "") : ref;
                      return (
                        <span
                          key={ref}
                          className="shrink-0 inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded-full border"
                          style={{
                            color: node.color,
                            borderColor: `color-mix(in srgb, ${node.color} 35%, transparent)`,
                            backgroundColor: `color-mix(in srgb, ${node.color} 10%, transparent)`,
                            boxShadow: `0 0 6px color-mix(in srgb, ${node.color} 15%, transparent)`,
                          }}
                        >
                          <svg width="8" height="8" viewBox="0 0 16 16" fill="currentColor">
                            <circle cx="8" cy="8" r="4" />
                          </svg>
                          {label}
                        </span>
                      );
                    })}

                  {/* Commit type badge */}
                  {type && (
                    <span
                      className="shrink-0 text-[9px] font-medium px-1 py-px rounded leading-none"
                      style={{
                        color: typeStyle.text,
                        backgroundColor: typeStyle.bg,
                      }}
                    >
                      {type}
                    </span>
                  )}

                  {/* Message: scope prefix + rest */}
                  <span className="truncate text-caption text-pir-text-secondary">
                    {scope && (
                      <span className="text-pir-text-tertiary font-mono">
                        {scope}:{" "}
                      </span>
                    )}
                    {rest || commit.message}
                  </span>

                  {/* Hash */}
                  <span className="shrink-0 font-mono text-[10px] text-pir-text-muted hidden md:inline opacity-60 hover:opacity-100 transition-opacity">
                    {commit.hash_short}
                  </span>

                  {/* Time */}
                  <span className="shrink-0 text-[10px] text-pir-text-muted tabular-nums ml-auto hidden md:inline">
                    {timeAgo(commit.date)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* Commit detail panel */}
      {selectedHash && (
        <div className="bg-pir-surface-0 border border-pir rounded p-4 space-y-3">
          {loadingDetail ? (
            <div className="text-caption text-pir-text-muted">
              Loading commit details...
            </div>
          ) : detail ? (
            <>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-body text-pir-text-primary font-medium leading-snug">
                    {detail.body.split("\n")[0]}
                  </div>
                  {detail.body.split("\n").length > 1 && (
                    <pre className="text-caption text-pir-text-tertiary mt-2 whitespace-pre-wrap font-mono leading-relaxed">
                      {detail.body.split("\n").slice(1).join("\n").trim()}
                    </pre>
                  )}
                </div>
                <button
                  onClick={() => {
                    setSelectedHash(null);
                    setDetail(null);
                  }}
                  className="shrink-0 p-1 rounded hover:bg-pir-surface-2 text-pir-text-muted hover:text-pir-text-secondary transition-colors"
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <path d="M4 4l8 8M12 4l-8 8" />
                  </svg>
                </button>
              </div>
              <div className="flex flex-wrap items-center gap-3 text-caption text-pir-text-tertiary">
                <span className="flex items-center gap-1">
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <circle cx="8" cy="5" r="3" />
                    <path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6" />
                  </svg>
                  {detail.author}
                </span>
                {/* Full hash with copy button */}
                <button
                  type="button"
                  onClick={handleCopyHash}
                  title="Copy full hash"
                  className="flex items-center gap-1 font-mono text-pir-text-muted hover:text-pir-text-secondary transition-colors group"
                >
                  <span>{detail.hash.slice(0, 12)}</span>
                  <span className="opacity-0 group-hover:opacity-100 transition-opacity">
                    {copiedHash ? (
                      <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" className="text-pir-success">
                        <path d="M3 8l4 4 6-6" />
                      </svg>
                    ) : (
                      <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                        <rect x="5" y="5" width="8" height="8" rx="1" />
                        <path d="M3 11V3h8" />
                      </svg>
                    )}
                  </span>
                </button>
                <span className="tabular-nums">{new Date(detail.date).toLocaleString()}</span>
              </div>
              {detail.stats.length > 0 && (
                <div className="border-t border-pir pt-3">
                  <div className="text-[10px] uppercase tracking-wider text-pir-text-muted font-medium mb-2">
                    Files changed
                  </div>
                  <div className="space-y-0.5">
                    {detail.stats.map((line, i) => (
                      <div
                        key={i}
                        className="text-caption font-mono text-pir-text-secondary py-0.5 px-2 rounded hover:bg-pir-surface-1 transition-colors"
                      >
                        {line}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="text-caption text-pir-error">
              Failed to load commit details.
            </div>
          )}
        </div>
      )}

      {/* Load more */}
      {hasMore && (
        <button
          onClick={loadMore}
          disabled={loadingMore}
          className="w-full py-2 text-caption text-pir-text-muted hover:text-pir-text-secondary bg-pir-surface-0 border border-pir rounded hover:border-pir-strong disabled:opacity-40 transition-colors duration-100"
        >
          {loadingMore ? "Loading..." : "Load more commits"}
        </button>
      )}

      {/* Branch list */}
      {refs.length > 0 && (
        <div>
          <h4 className="text-[10px] uppercase tracking-wider text-pir-text-muted font-medium mb-2">
            Refs
          </h4>
          <div className="flex flex-wrap gap-1.5">
            {refs.map((ref) => (
              <span
                key={ref.name}
                className={`text-[10px] font-mono font-medium px-2 py-0.5 rounded-full border transition-colors ${
                  ref.name === currentBranch
                    ? "bg-pir-accent/15 text-pir-accent border-pir-accent/25"
                    : ref.type === "tag"
                    ? "bg-pir-warning/15 text-pir-warning border-pir-warning/25"
                    : "bg-pir-surface-1 text-pir-text-muted border-pir hover:border-pir-strong"
                }`}
              >
                {ref.type === "tag" ? `tag: ${ref.name}` : ref.name}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Commit hover tooltip — rendered via portal to escape overflow:hidden containers */}
      {hoveredCommit && tooltipPos && typeof document !== "undefined" &&
        createPortal(
          <div
            className="pointer-events-none"
            style={{
              position: "fixed",
              left: tooltipPos.x + 14,
              top: tooltipPos.y - 8,
              zIndex: 9999,
            }}
          >
            <div
              className="rounded border border-zinc-600 bg-zinc-800 px-3 py-2 shadow-xl"
              style={{ maxWidth: 340, minWidth: 220 }}
            >
              {/* First line of message */}
              <div className="text-[12px] font-medium text-white leading-snug mb-1.5 truncate">
                {hoveredCommit.message.split("\n")[0]}
              </div>
              <div className="flex items-center gap-2 flex-wrap text-[10px] text-zinc-400">
                {/* Author */}
                <span className="flex items-center gap-1">
                  <svg width="9" height="9" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                    <circle cx="8" cy="5" r="3" />
                    <path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6" />
                  </svg>
                  {hoveredCommit.author}
                </span>
                {/* Relative date */}
                <span className="tabular-nums">{timeAgo(hoveredCommit.date)}</span>
                {/* Short hash */}
                <span className="font-mono text-zinc-500">{hoveredCommit.hash_short}</span>
              </div>
            </div>
          </div>,
          document.body
        )
      }
    </div>
  );
}
