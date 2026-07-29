"use client";

import { memo } from "react";
import type { DelegationType } from "@/lib/types";

const DELEGATION_OPTIONS: { value: DelegationType; label: string; color: string }[] = [
  { value: "agent", label: "Agent", color: "bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 border-emerald-500/30" },
  { value: "hybrid", label: "Hybrid", color: "bg-blue-500/20 text-blue-700 dark:text-blue-400 border-blue-500/30" },
  { value: "human", label: "Human", color: "bg-amber-500/20 text-amber-700 dark:text-amber-400 border-amber-500/30" },
];

interface ScoreTrackProps {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
}

function ScoreTrack({ label, value, onChange }: ScoreTrackProps) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-caption text-pir-text-tertiary">{label}</span>
        <span className="text-caption text-pir-text-muted tabular-nums w-4 text-right">
          {value ?? "-"}
        </span>
      </div>
      <div className="flex gap-px">
        {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => {
          const active = value != null && n <= value;
          const isSelected = n === value;
          return (
            <button
              key={n}
              onClick={() => onChange(n === value ? null : n)}
              className={`flex-1 h-5 text-[9px] rounded-sm transition-colors tabular-nums ${
                isSelected
                  ? "bg-pir-accent text-white"
                  : active
                    ? "bg-pir-accent/30 text-pir-accent"
                    : "bg-pir-surface-1 text-pir-text-muted hover:bg-pir-surface-2"
              }`}
            >
              {n}
            </button>
          );
        })}
      </div>
    </div>
  );
}

interface Props {
  impact: number | null;
  confidence: number | null;
  ease: number | null;
  delegation: DelegationType | null;
  iceScore: number | null;
  onChange: (fields: {
    impact?: number | null;
    confidence?: number | null;
    ease?: number | null;
    delegation?: DelegationType | null;
  }) => void;
}

const ScoreInput = memo(function ScoreInput({
  impact,
  confidence,
  ease,
  delegation,
  iceScore,
  onChange,
}: Props) {
  return (
    <div className="space-y-3">
      {/* ICE Score display */}
      <div className="flex items-center justify-between">
        <span className="text-label text-pir-text-secondary">ICE-D Score</span>
        {iceScore != null ? (
          <span className="text-heading text-pir-accent tabular-nums">{iceScore}</span>
        ) : (
          <span className="text-label text-pir-text-muted">Unscored</span>
        )}
      </div>

      {/* Score tracks */}
      <ScoreTrack label="Impact" value={impact} onChange={(v) => onChange({ impact: v })} />
      <ScoreTrack label="Confidence" value={confidence} onChange={(v) => onChange({ confidence: v })} />
      <ScoreTrack label="Ease" value={ease} onChange={(v) => onChange({ ease: v })} />

      {/* Delegation */}
      <div>
        <span className="text-caption text-pir-text-tertiary block mb-1">Delegation</span>
        <div className="flex gap-1">
          {DELEGATION_OPTIONS.map((opt) => {
            const active = delegation === opt.value;
            return (
              <button
                key={opt.value}
                onClick={() => onChange({ delegation: active ? null : opt.value })}
                className={`flex-1 text-[10px] py-1 rounded border transition-colors ${
                  active
                    ? opt.color
                    : "bg-pir-surface-1 text-pir-text-muted border-transparent hover:bg-pir-surface-2"
                }`}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
});

export default ScoreInput;
