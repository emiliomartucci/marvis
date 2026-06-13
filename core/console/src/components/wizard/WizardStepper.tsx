// Left-rail stepper. Indicator: pending (outlined) / active (filled) /
// completed (check) / skipped (dashed). Renders click-only on completed
// steps so users can review without breaking the linear flow.

"use client";

import { wizardActions, stepStatus } from "@/lib/wizard-store";
import { STEP_ORDER, type StepId, type WizardState } from "@/lib/wizard-schemas";

const STEP_LABELS: Record<StepId, string> = {
  welcome: "Welcome",
  storage: "Storage",
  llm_provider: "LLM provider",
  first_project: "First project",
  recap: "Review and finalize",
};

interface IndicatorProps {
  status: "active" | "completed" | "skipped" | "pending";
  index: number;
}

function Indicator({ status, index }: IndicatorProps) {
  const base =
    "flex h-7 w-7 items-center justify-center rounded-full text-[11px] font-semibold";
  if (status === "completed") {
    return (
      <span className={`${base} bg-pir-accent text-white`} aria-hidden="true">
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <polyline points="20 6 9 17 4 12" />
        </svg>
      </span>
    );
  }
  if (status === "active") {
    return (
      <span
        className={`${base} bg-pir-accent text-white ring-2 ring-pir-accent/30`}
      >
        {index + 1}
      </span>
    );
  }
  if (status === "skipped") {
    return (
      <span
        className={`${base} border border-dashed border-pir text-pir-text-muted`}
      >
        {index + 1}
      </span>
    );
  }
  return (
    <span className={`${base} border border-pir text-pir-text-muted`}>
      {index + 1}
    </span>
  );
}

interface WizardStepperProps {
  state: WizardState;
}

export default function WizardStepper({ state }: WizardStepperProps) {
  return (
    <nav className="w-60 shrink-0 pt-2" aria-label="Wizard steps">
      <ol className="space-y-1">
        {STEP_ORDER.map((step, idx) => {
          const status = stepStatus(state, step);
          const reviewable = status === "completed" || status === "skipped";
          return (
            <li key={step}>
              <button
                type="button"
                onClick={() => reviewable && wizardActions.jumpTo(step)}
                disabled={!reviewable && status !== "active"}
                className={[
                  "flex w-full items-center gap-3 rounded px-2 py-2 text-left transition-colors",
                  reviewable
                    ? "hover:bg-pir-surface-0"
                    : status === "active"
                      ? ""
                      : "cursor-not-allowed",
                ].join(" ")}
              >
                <Indicator status={status} index={idx} />
                <span
                  className={[
                    "text-sm",
                    status === "active"
                      ? "font-semibold text-pir-text-primary"
                      : status === "completed"
                        ? "text-pir-text-secondary"
                        : status === "skipped"
                          ? "italic text-pir-text-muted"
                          : "text-pir-text-muted",
                  ].join(" ")}
                >
                  {STEP_LABELS[step]}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
