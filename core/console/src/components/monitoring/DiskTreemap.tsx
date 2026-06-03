"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { getMonitoringDiskTree } from "@/lib/api";
import type { DiskTreeNode } from "@/lib/types";

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface RenderedNode {
  rect: Rect;
  node: DiskTreeNode;
  isParent: boolean;
}

function layoutHorizontal(nodes: DiskTreeNode[], r: Rect): RenderedNode[] {
  if (nodes.length === 0) return [];
  const total = nodes.reduce((s, n) => s + n.size_mb, 0);
  const result: RenderedNode[] = [];
  let x = r.x;
  for (const node of nodes) {
    const w = (node.size_mb / total) * r.w;
    result.push({ rect: { x, y: r.y, w, h: r.h }, node, isParent: false });
    x += w;
  }
  return result;
}

function layoutVertical(nodes: DiskTreeNode[], r: Rect): RenderedNode[] {
  if (nodes.length === 0) return [];
  const total = nodes.reduce((s, n) => s + n.size_mb, 0);
  const result: RenderedNode[] = [];
  let y = r.y;
  for (const node of nodes) {
    const h = (node.size_mb / total) * r.h;
    result.push({ rect: { x: r.x, y, w: r.w, h }, node, isParent: false });
    y += h;
  }
  return result;
}

const PATH_COLORS = [
  { bg: "#1e3a5f", fg: "#1e40af" },
  { bg: "#14532d", fg: "#065f46" },
  { bg: "#4c1d14", fg: "#7c2d12" },
  { bg: "#2d1b69", fg: "#4c1d95" },
  { bg: "#431407", fg: "#713f12" },
  { bg: "#1c1f2e", fg: "#334155" },
  { bg: "#1a2e1a", fg: "#166534" },
  { bg: "#2e1a1a", fg: "#7f1d1d" },
];

function buildLayout(items: DiskTreeNode[], W: number, H: number): RenderedNode[] {
  // depth=1 = direct children (parents), depth=2 = grandchildren (children inside parents)
  const parents = items.filter((n) => n.depth === 1).sort((a, b) => b.size_mb - a.size_mb);
  const children = items.filter((n) => n.depth === 2);

  const HEADER = 14;
  const PADDING = 2;
  const result: RenderedNode[] = [];

  const parentRects = layoutHorizontal(parents, { x: 0, y: 0, w: W, h: H });

  parentRects.forEach((pr, idx) => {
    result.push({ ...pr, isParent: true, node: { ...pr.node, _colorIdx: idx } as DiskTreeNode & { _colorIdx: number } });

    const kids = children
      .filter((n) => n.path.startsWith(pr.node.path + "/"))
      .sort((a, b) => b.size_mb - a.size_mb);

    if (kids.length === 0) return;

    const inner: Rect = {
      x: pr.rect.x + PADDING,
      y: pr.rect.y + HEADER,
      w: pr.rect.w - PADDING * 2,
      h: pr.rect.h - HEADER - PADDING,
    };

    if (inner.w < 4 || inner.h < 4) return;

    const childRects = layoutVertical(kids, inner);
    childRects.forEach((cr) => {
      result.push({ ...cr, isParent: false, node: { ...cr.node, _colorIdx: idx } as DiskTreeNode & { _colorIdx: number } });
    });
  });

  return result;
}

function formatMb(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${mb} MB`;
}

function pathParts(path: string): { label: string; path: string }[] {
  if (path === "/") return [{ label: "/", path: "/" }];
  const parts = path.split("/").filter(Boolean);
  const crumbs = [{ label: "/", path: "/" }];
  let current = "";
  for (const part of parts) {
    current += "/" + part;
    crumbs.push({ label: part, path: current });
  }
  return crumbs;
}

interface DiskTreemapProps {
  onClose: () => void;
}

export default function DiskTreemap({ onClose }: DiskTreemapProps) {
  const [currentPath, setCurrentPath] = useState("/");
  const [data, setData] = useState<{
    items: DiskTreeNode[];
    total_mb: number;
    free_mb: number;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [tooltip, setTooltip] = useState<{ text: string; x: number; y: number } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const load = useCallback((path: string) => {
    const controller = new AbortController();
    setLoading(true);
    setData(null);
    getMonitoringDiskTree({ path, signal: controller.signal })
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    return load(currentPath);
  }, [currentPath, load]);

  const navigateTo = (path: string) => {
    setCurrentPath(path);
    setTooltip(null);
  };

  const W = 640;
  const H = 280;
  const nodes = data ? buildLayout(data.items, W, H) : [];
  const breadcrumbs = pathParts(currentPath);

  return (
    <div className="space-y-2">
      {/* Header row: used/free + breadcrumb + close */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1 flex-wrap text-caption">
          {breadcrumbs.map((crumb, i) => (
            <span key={crumb.path} className="flex items-center gap-1">
              {i > 0 && <span className="text-pir-text-muted">/</span>}
              <button
                onClick={() => navigateTo(crumb.path)}
                className={`hover:text-pir-text-primary transition-colors ${
                  crumb.path === currentPath
                    ? "text-pir-text-primary font-medium"
                    : "text-pir-text-muted"
                }`}
              >
                {crumb.label}
              </button>
            </span>
          ))}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {data && (
            <span className="text-caption text-pir-text-muted">
              <span className="text-pir-text-primary">{formatMb(data.total_mb - data.free_mb)}</span>
              {" / "}{formatMb(data.total_mb)}{" — "}
              <span className="text-green-400">{formatMb(data.free_mb)} free</span>
            </span>
          )}
          <button onClick={onClose} className="text-caption text-pir-text-muted hover:text-pir-text-primary">
            close
          </button>
        </div>
      </div>

      <div className="relative w-full overflow-hidden rounded border border-pir">
        {loading ? (
          <div className="flex items-center justify-center h-[200px] text-caption text-pir-text-muted">
            Loading {currentPath}...
          </div>
        ) : !data || data.items.length === 0 ? (
          <div className="flex items-center justify-center h-[160px] text-caption text-pir-text-muted">
            No data for {currentPath}
          </div>
        ) : (
          <svg
            ref={svgRef}
            viewBox={`0 0 ${W} ${H}`}
            className="w-full h-auto"
            style={{ display: "block" }}
            onMouseLeave={() => setTooltip(null)}
          >
            {nodes.map((n, i) => {
              const colorIdx = ((n.node as DiskTreeNode & { _colorIdx?: number })._colorIdx ?? i) % PATH_COLORS.length;
              const color = n.isParent
                ? PATH_COLORS[colorIdx].bg
                : PATH_COLORS[colorIdx].fg;
              const { x, y, w, h } = n.rect;
              return (
                <g key={i}>
                  <rect
                    x={x + 0.5}
                    y={y + 0.5}
                    width={Math.max(0, w - 1)}
                    height={Math.max(0, h - 1)}
                    fill={color}
                    stroke={n.isParent ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.25)"}
                    strokeWidth={n.isParent ? 1 : 0.5}
                    className="cursor-pointer hover:brightness-125 transition-all"
                    onMouseEnter={(e) => {
                      const svgRect = svgRef.current?.getBoundingClientRect();
                      if (!svgRect) return;
                      const svgX = ((e.clientX - svgRect.left) / svgRect.width) * W;
                      const svgY = ((e.clientY - svgRect.top) / svgRect.height) * H;
                      setTooltip({
                        text: `${n.node.path}  ${formatMb(n.node.size_mb)} — click to explore`,
                        x: svgX,
                        y: svgY,
                      });
                    }}
                    onClick={() => navigateTo(n.node.path)}
                  />
                  {/* Parent: label at top */}
                  {n.isParent && w > 28 && h > 14 && (
                    <text
                      x={x + w / 2}
                      y={y + 9}
                      textAnchor="middle"
                      fill="rgba(255,255,255,0.65)"
                      fontSize={Math.min(11, w / 5)}
                      fontWeight="500"
                      className="pointer-events-none select-none"
                    >
                      {n.node.name}
                    </text>
                  )}
                  {/* Child: label centered */}
                  {!n.isParent && w > 50 && h > 16 && (
                    <text
                      x={x + w / 2}
                      y={y + h / 2}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      fill="rgba(255,255,255,0.7)"
                      fontSize={Math.min(10, w / 6, h / 2.5)}
                      className="pointer-events-none select-none"
                    >
                      {n.node.name}
                    </text>
                  )}
                </g>
              );
            })}

            {tooltip && (
              <g>
                <rect
                  x={Math.min(tooltip.x + 4, W - 260)}
                  y={Math.max(tooltip.y - 22, 4)}
                  width={255}
                  height={18}
                  fill="rgba(0,0,0,0.9)"
                  rx={3}
                />
                <text
                  x={Math.min(tooltip.x + 8, W - 256)}
                  y={Math.max(tooltip.y - 9, 16)}
                  fill="rgba(255,255,255,0.9)"
                  fontSize={10}
                  className="pointer-events-none"
                >
                  {tooltip.text}
                </text>
              </g>
            )}
          </svg>
        )}
      </div>
    </div>
  );
}
