"use client";

import { CandleDatapoint } from "@/lib/types";

interface SummaryStats {
  min: number;
  p25: number;
  mean: number;
  p75: number;
  max: number;
  count: number;
}

function computeStats(candles: CandleDatapoint[]): SummaryStats | null {
  if (candles.length === 0) return null;

  const closes = candles.map((c) => c.c).sort((a, b) => a - b);
  const n = closes.length;

  const min = closes[0];
  const max = closes[n - 1];
  const mean = closes.reduce((s, v) => s + v, 0) / n;
  const p25 = closes[Math.floor(n * 0.25)];
  const p75 = closes[Math.floor(n * 0.75)];

  return { min, p25, mean, p75, max, count: n };
}

interface SummaryCandleProps {
  candles: CandleDatapoint[];
  /** Fixed Y domain [min, max], defaults to [0, 100] */
  yDomain?: [number, number];
  width?: number;
  height?: number;
  className?: string;
}

export default function SummaryCandle({
  candles,
  yDomain = [0, 100],
  width = 28,
  height = 80,
  className = "",
}: SummaryCandleProps) {
  const stats = computeStats(candles);

  const toY = (v: number) => {
    const [lo, hi] = yDomain;
    const ratio = 1 - (v - lo) / (hi - lo);
    return Math.max(0, Math.min(height, ratio * height));
  };

  const cx = width / 2;
  const boxW = width * 0.5;
  const boxX = cx - boxW / 2;

  return (
    <div className={`flex flex-col items-center gap-1 ${className}`}>
      <svg width={width} height={height} className="overflow-visible">
        {stats ? (
          <>
            {/* Wick: min → max */}
            <line
              x1={cx}
              y1={toY(stats.max)}
              x2={cx}
              y2={toY(stats.min)}
              stroke="#4b5563"
              strokeWidth={1}
            />
            {/* IQR box: p25 → p75 */}
            <rect
              x={boxX}
              y={toY(stats.p75)}
              width={boxW}
              height={Math.abs(toY(stats.p25) - toY(stats.p75))}
              fill="#1e40af"
              fillOpacity={0.5}
              stroke="#3b82f6"
              strokeWidth={0.5}
              rx={1}
            />
            {/* Mean line */}
            <line
              x1={boxX}
              y1={toY(stats.mean)}
              x2={boxX + boxW}
              y2={toY(stats.mean)}
              stroke="#60a5fa"
              strokeWidth={1.5}
            />
          </>
        ) : (
          <line
            x1={cx}
            y1={0}
            x2={cx}
            y2={height}
            stroke="#374151"
            strokeWidth={1}
            strokeDasharray="3,2"
          />
        )}
      </svg>
      {stats && (
        <div className="text-[10px] text-pir-text-muted text-center leading-tight">
          <div>{stats.mean.toFixed(1)}</div>
          <div className="text-[9px] opacity-60">{stats.min.toFixed(0)}–{stats.max.toFixed(0)}</div>
        </div>
      )}
    </div>
  );
}

export { computeStats };
export type { SummaryStats };
