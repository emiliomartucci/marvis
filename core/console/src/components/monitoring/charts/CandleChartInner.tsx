"use client";

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  CandlestickSeries,
  LineSeries,
  ColorType,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { getMonitoringHistory } from "@/lib/api";
import type { CandleDatapoint } from "@/lib/types";
import TimeframeSelector from "./TimeframeSelector";
import SummaryCandle from "./SummaryCandle";

interface Props {
  metric: string;
  initialRange?: "24h" | "7d" | "30d";
  /** If set, Y axis is fixed to this domain */
  fixedYRange?: [number, number];
  /** "candle" for OHLC (CPU), "line" for smooth trend (RAM) */
  chartType?: "candle" | "line";
}

const CHART_COLOR = "hsl(213 70% 55%)";

export default function CandleChartInner({
  metric,
  initialRange = "24h",
  fixedYRange,
  chartType = "candle",
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<
    ISeriesApi<"Candlestick"> | ISeriesApi<"Line"> | null
  >(null);
  const [range, setRange] = useState<"24h" | "7d" | "30d">(initialRange);
  const [loading, setLoading] = useState(false);
  const [candles, setCandles] = useState<CandleDatapoint[]>([]);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "hsl(220 20% 9%)" },
        textColor: "rgba(255,255,255,0.5)",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.04)" },
        horzLines: { color: "rgba(255,255,255,0.04)" },
      },
      timeScale: {
        timeVisible: true,
        borderColor: "rgba(255,255,255,0.08)",
      },
      rightPriceScale: {
        borderColor: "rgba(255,255,255,0.08)",
      },
    });

    const baseOpts = fixedYRange
      ? {
          autoscaleInfoProvider: () => ({
            priceRange: { minValue: fixedYRange[0], maxValue: fixedYRange[1] },
            margins: { above: 0.05, below: 0.05 },
          }),
        }
      : {};

    let series: ISeriesApi<"Candlestick"> | ISeriesApi<"Line">;

    if (chartType === "line") {
      series = chart.addSeries(LineSeries, {
        ...baseOpts,
        color: CHART_COLOR,
        lineWidth: 2,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 3,
        priceLineVisible: false,
      });
    } else {
      series = chart.addSeries(CandlestickSeries, {
        ...baseOpts,
        upColor: CHART_COLOR,
        downColor: CHART_COLOR,
        borderUpColor: CHART_COLOR,
        borderDownColor: CHART_COLOR,
        wickUpColor: "rgba(148,163,184,0.6)",
        wickDownColor: "rgba(148,163,184,0.6)",
      });
    }

    chartRef.current = chart;
    seriesRef.current = series;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartType]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);

    getMonitoringHistory(metric, range, { signal: controller.signal })
      .then((data: CandleDatapoint[]) => {
        if (!seriesRef.current) return;

        if (chartType === "line") {
          (seriesRef.current as ISeriesApi<"Line">).setData(
            data.map((d) => ({ time: d.t as UTCTimestamp, value: d.c }))
          );
        } else {
          (seriesRef.current as ISeriesApi<"Candlestick">).setData(
            data.map((d) => ({
              time: d.t as UTCTimestamp,
              open: d.o,
              high: d.h,
              low: d.l,
              close: d.c,
            }))
          );
        }

        chartRef.current?.timeScale().fitContent();
        setCandles(data);
      })
      .catch(() => {})
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, [metric, range, chartType]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-caption text-pir-text-muted font-mono">
          {metric}
        </span>
        <TimeframeSelector value={range} onChange={setRange} />
      </div>
      <div className="flex items-stretch gap-2">
        <div className="relative flex-1" style={{ height: 200 }}>
          <div ref={containerRef} className="w-full h-full" />
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-pir-base/40">
              <span className="text-caption text-pir-text-muted">Loading...</span>
            </div>
          )}
        </div>
        <div className="flex items-center border-l border-pir pl-2">
          <SummaryCandle
            candles={candles}
            yDomain={fixedYRange ?? [0, 100]}
            width={32}
            height={160}
          />
        </div>
      </div>
    </div>
  );
}
