"use client";

import dynamic from "next/dynamic";
import type { ComponentProps } from "react";
import type CandleChartInner from "./CandleChartInner";

const CandleChartDynamic = dynamic(() => import("./CandleChartInner"), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-[200px] border border-pir rounded">
      <span className="text-caption text-pir-text-muted">Loading chart...</span>
    </div>
  ),
});

export default function CandleChart(
  props: ComponentProps<typeof CandleChartInner>
) {
  return <CandleChartDynamic {...props} />;
}
