// v2.0.0 - 2026-05-17 - Codex lens canvas con tokens var(--pir-*) light-mode safe
"use client";

import { useMemo, useState, type CSSProperties } from "react";

import type {
  ModifiedFunctionItem,
  TouchKind,
  TransitiveImpactItem,
} from "./types";

const SVG_VIEWBOX = 1000;

const TOUCH_KIND_FILL: Record<TouchKind, string> = {
  add: "hsl(var(--pir-success))",
  modify: "hsl(var(--pir-info))",
  delete: "hsl(var(--pir-error))",
};

interface NodePosition {
  cx: number;
  cy: number;
  r: number;
}

function fibonacciAngles(count: number, startOffset = 0): number[] {
  if (count <= 0) return [];
  const phi = Math.PI * (3 - Math.sqrt(5));
  const angles: number[] = new Array(count);
  for (let i = 0; i < count; i++) {
    angles[i] = startOffset + i * phi;
  }
  return angles;
}

function ringPosition(
  index: number,
  total: number,
  ringRadius: number,
  startOffset = 0
): NodePosition {
  const angles = fibonacciAngles(total, startOffset);
  const a = angles[index];
  const cx = SVG_VIEWBOX / 2 + ringRadius * Math.cos(a);
  const cy = SVG_VIEWBOX / 2 + ringRadius * Math.sin(a);
  return { cx, cy, r: 0 };
}

function satelliteRadius(weight: number): number {
  return 8 + weight * 16;
}

export interface PrImpactCanvasProps {
  modifiedFunctions: ModifiedFunctionItem[];
  transitiveImpact: TransitiveImpactItem[];
  selectedNodeId: string | null;
  filterKinds: Set<TouchKind>;
  onNodeClick: (nodeId: string, fn: ModifiedFunctionItem | null) => void;
}

export function PrImpactCanvas({
  modifiedFunctions,
  transitiveImpact,
  selectedNodeId,
  filterKinds,
  onNodeClick,
}: PrImpactCanvasProps) {
  const visibleModified = useMemo(
    () =>
      modifiedFunctions.filter(
        (m) => filterKinds.size === 0 || filterKinds.has(m.touch_kind)
      ),
    [modifiedFunctions, filterKinds]
  );

  const modifiedPositions = useMemo(() => {
    const innerRadius = 220;
    return visibleModified.map((m, i): NodePosition & { fn: ModifiedFunctionItem } => {
      const pos = ringPosition(i, visibleModified.length, innerRadius);
      return { ...pos, r: satelliteRadius(m.weight), fn: m };
    });
  }, [visibleModified]);

  const transitivePositions = useMemo(() => {
    const outerRadius = 380;
    return transitiveImpact.map((t, i): NodePosition & {
      transitive: TransitiveImpactItem;
    } => {
      const pos = ringPosition(i, transitiveImpact.length, outerRadius, Math.PI / 6);
      return { ...pos, r: 6, transitive: t };
    });
  }, [transitiveImpact]);

  const centerX = SVG_VIEWBOX / 2;
  const centerY = SVG_VIEWBOX / 2;
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  return (
    <svg
      viewBox={`0 0 ${SVG_VIEWBOX} ${SVG_VIEWBOX}`}
      style={CANVAS_STYLE}
      role="img"
      aria-label="Codex impact graph"
    >
      <circle cx={centerX} cy={centerY} r={220} stroke="var(--pir-border)" fill="none" />
      <circle cx={centerX} cy={centerY} r={380} stroke="var(--pir-border)" fill="none" />

      <g>
        {modifiedPositions.map((p) => (
          <line
            key={`pr-edge-${p.fn.node_id}`}
            x1={centerX}
            y1={centerY}
            x2={p.cx}
            y2={p.cy}
            stroke="hsl(var(--pir-accent) / 0.35)"
            strokeWidth={1 + p.fn.weight * 2}
            opacity={selectedNodeId && selectedNodeId !== p.fn.node_id ? 0.2 : 1}
          />
        ))}
      </g>

      <g>
        {transitivePositions.map((p) => {
          const closest = nearestModified(modifiedPositions, p.cx, p.cy);
          if (!closest) return null;
          return (
            <line
              key={`tr-edge-${p.transitive.node_id}-${p.transitive.via_edge}`}
              x1={closest.cx}
              y1={closest.cy}
              x2={p.cx}
              y2={p.cy}
              stroke="var(--pir-border-strong)"
              strokeWidth={0.5}
              opacity={0.6}
            />
          );
        })}
      </g>

      <g>
        <circle cx={centerX} cy={centerY} r={60} fill="hsl(var(--pir-accent) / 0.18)" />
        <circle
          cx={centerX}
          cy={centerY}
          r={38}
          fill="hsl(var(--pir-accent))"
          stroke="hsl(var(--pir-surface-0))"
          strokeWidth={2}
        />
        <text
          x={centerX}
          y={centerY + 6}
          textAnchor="middle"
          fontFamily="var(--pir-font-mono, 'JetBrains Mono', monospace)"
          fontSize={18}
          fontWeight={700}
          fill="hsl(var(--pir-base))"
        >
          PR
        </text>
      </g>

      <g>
        {modifiedPositions.map((p) => {
          const isSelected = p.fn.node_id === selectedNodeId;
          const isHovered = p.fn.node_id === hoveredId;
          const fill = TOUCH_KIND_FILL[p.fn.touch_kind];
          return (
            <g
              key={`mod-${p.fn.node_id}`}
              style={{ cursor: "pointer" }}
              onMouseEnter={() => setHoveredId(p.fn.node_id)}
              onMouseLeave={() => setHoveredId(null)}
              onClick={() => onNodeClick(p.fn.node_id, p.fn)}
            >
              <circle
                cx={p.cx}
                cy={p.cy}
                r={p.r + 4}
                fill={fill}
                opacity={isSelected || isHovered ? 0.4 : 0.15}
              />
              <circle
                cx={p.cx}
                cy={p.cy}
                r={p.r}
                fill={fill}
                stroke={isSelected ? "var(--pir-text-primary)" : "var(--pir-border-strong)"}
                strokeWidth={isSelected ? 3 : 1}
              />
              {(isHovered || isSelected) && (
                <FunctionLabel
                  cx={p.cx}
                  cy={p.cy}
                  r={p.r}
                  text={shortenQualifiedName(p.fn.qualified_name_snapshot)}
                />
              )}
            </g>
          );
        })}
      </g>

      <g>
        {transitivePositions.map((p) => {
          const isHovered = p.transitive.node_id === hoveredId;
          return (
            <g
              key={`tr-${p.transitive.node_id}-${p.transitive.via_edge}`}
              style={{ cursor: "pointer" }}
              onMouseEnter={() => setHoveredId(p.transitive.node_id)}
              onMouseLeave={() => setHoveredId(null)}
              onClick={() => onNodeClick(p.transitive.node_id, null)}
            >
              <circle
                cx={p.cx}
                cy={p.cy}
                r={p.r}
                fill="var(--pir-text-tertiary)"
                opacity={isHovered ? 0.9 : 0.55}
                stroke="var(--pir-border)"
                strokeWidth={0.75}
              />
              {isHovered && (
                <FunctionLabel
                  cx={p.cx}
                  cy={p.cy}
                  r={p.r}
                  text={shortenQualifiedName(p.transitive.node_id)}
                />
              )}
            </g>
          );
        })}
      </g>

      {modifiedFunctions.length === 0 && (
        <text
          x={centerX}
          y={centerY + 100}
          textAnchor="middle"
          fontFamily="var(--pir-font-sans, 'IBM Plex Sans', sans-serif)"
          fontSize={18}
          fill="var(--pir-text-tertiary)"
        >
          Nessuna funzione toccata — il populator potrebbe ancora girare
        </text>
      )}
    </svg>
  );
}

const CANVAS_STYLE: CSSProperties = {
  width: "100%",
  height: "100%",
  background:
    "radial-gradient(circle at center, hsl(var(--pir-accent) / 0.06) 0%, transparent 60%), hsl(var(--pir-surface-0))",
  display: "block",
};

function FunctionLabel({
  cx,
  cy,
  r,
  text,
}: {
  cx: number;
  cy: number;
  r: number;
  text: string;
}) {
  const textY = cy + r + 16;
  return (
    <g>
      <rect
        x={cx - text.length * 4}
        y={textY - 12}
        width={text.length * 8}
        height={18}
        rx={3}
        fill="hsl(var(--pir-surface-2))"
        stroke="var(--pir-border)"
      />
      <text
        x={cx}
        y={textY + 2}
        textAnchor="middle"
        fontFamily="var(--pir-font-mono, 'JetBrains Mono', monospace)"
        fontSize={11}
        fill="var(--pir-text-primary)"
      >
        {text}
      </text>
    </g>
  );
}

function shortenQualifiedName(name: string): string {
  const stripped = name
    .replace(/^py:function:/, "")
    .replace(/^ts:function:/, "")
    .replace(/^py:file:/, "")
    .replace(/^ts:file:/, "");
  if (stripped.length <= 32) return stripped;
  return "…" + stripped.slice(-31);
}

function nearestModified(
  modifiedPositions: Array<NodePosition & { fn: ModifiedFunctionItem }>,
  x: number,
  y: number
): NodePosition | null {
  if (modifiedPositions.length === 0) return null;
  let best = modifiedPositions[0];
  let bestDist = Number.POSITIVE_INFINITY;
  for (const p of modifiedPositions) {
    const dx = p.cx - x;
    const dy = p.cy - y;
    const d = dx * dx + dy * dy;
    if (d < bestDist) {
      bestDist = d;
      best = p;
    }
  }
  return best;
}
