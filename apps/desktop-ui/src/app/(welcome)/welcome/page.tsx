"use client";

import { useWizardStore, useHydrated } from "@/lib/wizard-store";
import WizardStepper from "@/components/wizard/WizardStepper";
import WelcomeStep from "@/components/wizard/steps/WelcomeStep";
import StorageStep from "@/components/wizard/steps/StorageStep";
import LlmProviderStep from "@/components/wizard/steps/LlmProviderStep";
import FirstProjectStep from "@/components/wizard/steps/FirstProjectStep";
import RecapStep from "@/components/wizard/steps/RecapStep";
import { Logo } from "@/components/ui/Logo";
import type { StepId } from "@/lib/wizard-schemas";

const STEPS: Record<StepId, () => React.JSX.Element> = {
  welcome: WelcomeStep,
  storage: StorageStep,
  llm_provider: LlmProviderStep,
  first_project: FirstProjectStep,
  recap: RecapStep,
};

export default function WelcomePage() {
  const state = useWizardStore();
  const hydrated = useHydrated();
  const ActiveStep = STEPS[state.current_step];

  // Avoid hydration mismatch when persisted state diverges from initial.
  const safeState = hydrated ? state : { ...state, current_step: "welcome" as const };

  return (
    <>
      <header className="flex justify-center px-6 py-6">
        <Logo size="md" />
      </header>

      <main className="mx-auto flex w-full max-w-5xl flex-1 gap-8 px-6 pb-6">
        <WizardStepper state={safeState} />
        <section className="flex flex-1 justify-center">
          <div className="w-full max-w-xl">
            <ActiveStep />
          </div>
        </section>
      </main>

      <footer className="border-t border-pir px-6 py-3 text-xs text-pir-text-muted">
        <div className="mx-auto flex max-w-5xl items-center justify-between">
          <span>MarvisX wizard v1.0</span>
          <button
            type="button"
            className="text-pir-text-muted underline-offset-4 hover:text-pir-text-secondary hover:underline"
            // Wave 2 wires skip-all confirm modal + finalize with skipped_steps=[storage,llm,project]
            disabled
          >
            Skip all setup
          </button>
        </div>
      </footer>
    </>
  );
}
