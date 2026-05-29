// Back / skip / continue triplet. Skip hidden when current step is not skippable.

"use client";

import { wizardActions, useWizardStore } from "@/lib/wizard-store";
import { SKIPPABLE_STEPS, STEP_ORDER } from "@/lib/wizard-schemas";

interface WizardControlsProps {
  /** Optional gate; if false the Continue button is disabled. */
  canContinue?: boolean;
  /** Label override; defaults to "Continue". */
  continueLabel?: string;
  /** Custom action on Continue; defaults to wizardActions.advance(). */
  onContinue?: () => void;
}

export default function WizardControls({
  canContinue = true,
  continueLabel = "Continue",
  onContinue,
}: WizardControlsProps) {
  const state = useWizardStore();
  const idx = STEP_ORDER.indexOf(state.current_step);
  const canSkip = SKIPPABLE_STEPS.has(state.current_step);
  const canGoBack = idx > 0;

  const handleContinue = () => {
    if (onContinue) {
      onContinue();
    } else {
      wizardActions.advance();
    }
  };

  return (
    <>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => wizardActions.goBack()}
          disabled={!canGoBack}
          className="rounded border border-pir px-3 py-2 text-sm text-pir-text-secondary transition-colors hover:bg-pir-base disabled:cursor-not-allowed disabled:opacity-40"
        >
          Back
        </button>
        {canSkip ? (
          <button
            type="button"
            onClick={() => wizardActions.skipCurrent()}
            className="rounded px-3 py-2 text-sm text-pir-text-muted underline-offset-4 transition-colors hover:text-pir-text-secondary hover:underline"
          >
            Configure later
          </button>
        ) : null}
      </div>

      <button
        type="button"
        onClick={handleContinue}
        disabled={!canContinue}
        className="rounded bg-pir-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-pir-accent/90 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {continueLabel}
      </button>
    </>
  );
}
