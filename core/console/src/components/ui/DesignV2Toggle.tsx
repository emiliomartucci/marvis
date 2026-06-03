"use client";

import { useDesignV2, setDesignV2 } from "@/lib/useDesignV2";

export function DesignV2Toggle() {
  const enabled = useDesignV2();
  return (
    <button
      type="button"
      onClick={() => setDesignV2(!enabled)}
      className="inline-flex items-center gap-2 px-3 py-1.5 text-[11px] uppercase tracking-[0.14em] font-medium border border-pir text-pir-text-secondary hover:border-pir-strong hover:text-pir-text-primary transition-colors rounded"
      aria-pressed={enabled}
      aria-label="Toggle design system v2"
      title={`Design System v2 is ${enabled ? "ON" : "OFF"}`}
    >
      <span>DS v2</span>
      <span className={enabled ? "text-pir-accent" : "text-pir-text-muted"}>
        {enabled ? "ON" : "OFF"}
      </span>
      <span className="text-[9px] px-1 py-0.5 border border-pir-strong rounded-sm text-pir-text-tertiary">
        BETA
      </span>
    </button>
  );
}
