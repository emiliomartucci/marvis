"use client";

import type { MetricDatapoint } from "@/lib/types";

interface Props {
  label: string;
  value: string;
  unit?: string;
  sparkline?: MetricDatapoint[];
  alert?: boolean;
}

export default function MetricCard({ label, value, unit, sparkline, alert }: Props) {
  return (
    <div
      className={`border rounded p-3 ${
        alert
          ? "border-yellow-500/40 bg-yellow-500/5"
          : "border-pir bg-pir-surface-0"
      }`}
    >
      <div className="text-caption text-pir-text-muted mb-1">{label}</div>
      <div className="flex items-end justify-between gap-2">
        <div className="flex items-baseline gap-1">
          <span className="text-xl font-mono tabular-nums text-pir-text-primary">
            {value}
          </span>
          {unit && (
            <span className="text-caption text-pir-text-muted">{unit}</span>
          )}
        </div>
        {sparkline && sparkline.length > 1 && (
          <MiniSparkline data={sparkline} alert={alert} />
        )}
      </div>
    </div>
  );
}

function MiniSparkline({
  data,
  alert,
}: {
  data: MetricDatapoint[];
  alert?: boolean;
}) {
  const w = 80;
  const h = 28;
  const values = data.map((d) => d.v);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = h - ((v - min) / range) * (h - 2) - 1;
      return `${x},${y}`;
    })
    .join(" ");

  const fillPoints = `0,${h} ${points} ${w},${h}`;

  const strokeColor = alert
    ? "rgba(234, 179, 8, 0.8)"
    : "rgba(96, 165, 250, 0.8)";
  const fillColor = alert
    ? "rgba(234, 179, 8, 0.1)"
    : "rgba(96, 165, 250, 0.1)";

  return (
    <svg width={w} height={h} className="shrink-0">
      <polygon points={fillPoints} fill={fillColor} />
      <polyline
        points={points}
        fill="none"
        stroke={strokeColor}
        strokeWidth="1.5"
      />
    </svg>
  );
}
