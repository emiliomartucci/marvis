"use client";

// Brain v1.1 — EU AI Act Art. 50 transparency badge.
// Renders inline next to any LLM-polished field (journal narrative, finding
// summary/why_now/reasoning). Hover tooltip exposes the cited evidence_refs
// used to ground the polish call.

import type { FC } from "react";

const DEFAULT_MODEL_LABEL = "Gemma 3 12B QAT";

/** @public */
export type LLMPolishBadgeProps = {
  polished: boolean;
  citedRefs?: string[];
  model?: string;
};

/** @public */
export const LLMPolishBadge: FC<LLMPolishBadgeProps> = ({
  polished,
  citedRefs,
  model,
}) => {
  if (!polished) {
    return null;
  }
  const refs = (citedRefs ?? []).filter(Boolean);
  const refsLabel = refs.length > 0 ? refs.join(", ") : "(no refs)";
  const modelLabel = model && model.length > 0 ? model : DEFAULT_MODEL_LABEL;
  const tooltip = `Polished by ${modelLabel} via Mac Gateway. Grounding refs: ${refsLabel}`;
  return (
    <span
      className="ml-2 inline-flex items-center gap-1 align-middle font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.18em] text-pir-text-tertiary opacity-70 hover:opacity-100"
      title={tooltip}
      aria-label={tooltip}
      data-testid="llm-polish-badge"
    >
      <span aria-hidden>↗</span>
      <span>Gemma polish</span>
    </span>
  );
};
