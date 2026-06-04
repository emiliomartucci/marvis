// v1.0.0 - 2026-05-17 - Codex zoom view: function planets inside one module
"use client";

import { useMemo, useState, type CSSProperties } from "react";

import { CLUSTER_HSL, type CodexClusterId, type CodexFunctionItem } from "./types";

const SVG_VIEWBOX = 1000;
const CENTER = SVG_VIEWBOX / 2;

export interface CodexFunctionsCanvasProps {
  functions: CodexFunctionItem[];
  module: string;
  cluster: CodexClusterId;
  onSelect: (fn: CodexFunctionItem) => void;
  selectedNodeId: string | null;
}

export function CodexFunctionsCanvas({
  functions,
  module,
  cluster,
  onSelect,
  selectedNodeId,
}: CodexFunctionsCanvasProps) {
  const positions = useMemo(() => {
    const phi = Math.PI * (3 - Math.sqrt(5));
    const baseRadius = 90;
    const ringStep = 60;
    return functions.map((fn, i) => {
      const ringIndex = Math.floor(i / 12);
      const radius = baseRadius + ringIndex * ringStep + (i % 12) * 10;
      const angle = i * phi;
      return {
        cx: CENTER + radius * Math.cos(angle),
        cy: CENTER + radius * Math.sin(angle),
        r: functionRadius(fn.touch_count_7d, fn.touch_count_30d),
        fn,
      };
    });
  }, [functions]);

  const [hovered, setHovered] = useState<string | null>(null);
  const clusterColor = CLUSTER_HSL[cluster];

  return (
    <svg
      viewBox={`0 0 ${SVG_VIEWBOX} ${SVG_VIEWBOX}`}
      style={CANVAS_STYLE}
      role="img"
      aria-label={`Funzioni del modulo ${module}`}
    >
      <text
        x={CENTER}
        y={42}
        textAnchor="middle"
        fontFamily="var(--pir-font-mono, monospace)"
        fontSize={11}
        fontWeight={700}
        letterSpacing="0.24em"
        style={{ textTransform: "uppercase" }}
        fill="var(--pir-text-tertiary)"
      >
        modulo · {module}
      </text>
      <text
        x={CENTER}
        y={58}
        textAnchor="middle"
        fontFamily="var(--pir-font-sans, sans-serif)"
        fontSize={11}
        fill="var(--pir-text-muted)"
      >
        {functions.length} funzioni
      </text>

      {/* Cluster halo at center */}
      <circle
        cx={CENTER}
        cy={CENTER}
        r={64}
        fill={clusterColor}
        opacity={0.14}
      />
      <circle
        cx={CENTER}
        cy={CENTER}
        r={40}
        fill={clusterColor}
        stroke="hsl(var(--pir-surface-0))"
        strokeWidth={2}
      />
      <text
        x={CENTER}
        y={CENTER + 4}
        textAnchor="middle"
        fontFamily="var(--pir-font-mono, monospace)"
        fontSize={14}
        fontWeight={700}
        fill="hsl(var(--pir-base))"
      >
        {cluster.toUpperCase()}
      </text>

      {positions.map((p) => {
        const isSelected = p.fn.node_id === selectedNodeId;
        const isHovered = p.fn.node_id === hovered;
        const hot = p.fn.touch_count_7d > 0;
        const ringFill = hot
          ? "hsl(var(--pir-warning))"
          : "var(--pir-text-tertiary)";
        return (
          <g
            key={p.fn.node_id}
            style={{ cursor: "pointer" }}
            onMouseEnter={() => setHovered(p.fn.node_id)}
            onMouseLeave={() => setHovered(null)}
            onClick={() => onSelect(p.fn)}
          >
            <circle
              cx={p.cx}
              cy={p.cy}
              r={p.r + 4}
              fill={ringFill}
              opacity={isSelected || isHovered ? 0.28 : 0.1}
            />
            <circle
              cx={p.cx}
              cy={p.cy}
              r={p.r}
              fill={ringFill}
              stroke={
                isSelected
                  ? "var(--pir-text-primary)"
                  : "var(--pir-border-strong)"
              }
              strokeWidth={isSelected ? 2.5 : 0.75}
            />
            {(isSelected || isHovered) && (
              <FunctionLabel
                cx={p.cx}
                cy={p.cy}
                r={p.r}
                text={shortenName(p.fn.qualified_name)}
              />
            )}
          </g>
        );
      })}

      {functions.length === 0 && (
        <text
          x={CENTER}
          y={CENTER + 100}
          textAnchor="middle"
          fontFamily="var(--pir-font-sans, sans-serif)"
          fontSize={14}
          fill="var(--pir-text-tertiary)"
        >
          Modulo senza funzioni indicizzate (potrebbe essere solo file).
        </text>
      )}
    </svg>
  );
}

function functionRadius(touch7d: number, touch30d: number): number {
  // Hot functions get bigger satellites so the eye picks them up.
  const hot = Math.min(20, touch7d * 4);
  const warm = Math.min(8, touch30d * 0.6);
  return 6 + hot + warm;
}

function shortenName(name: string): string {
  const stripped = name
    .replace(/^py:function:/, "")
    .replace(/^ts:function:/, "");
  if (stripped.length <= 36) return stripped;
  return "…" + stripped.slice(-35);
}

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
  const textY = cy + r + 14;
  return (
    <g>
      <rect
        x={cx - text.length * 3.6}
        y={textY - 11}
        width={text.length * 7.2}
        height={17}
        rx={3}
        fill="hsl(var(--pir-surface-2))"
        stroke="var(--pir-border)"
      />
      <text
        x={cx}
        y={textY + 1}
        textAnchor="middle"
        fontFamily="var(--pir-font-mono, monospace)"
        fontSize={10}
        fill="var(--pir-text-primary)"
      >
        {text}
      </text>
    </g>
  );
}

const CANVAS_STYLE: CSSProperties = {
  width: "100%",
  height: "100%",
  background:
    "radial-gradient(circle at center, hsl(var(--pir-accent) / 0.06) 0%, transparent 60%), hsl(var(--pir-surface-0))",
  display: "block",
};
