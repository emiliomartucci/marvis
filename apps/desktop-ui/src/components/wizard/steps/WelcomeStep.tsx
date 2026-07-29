"use client";

import { wizardActions, useWizardStore } from "@/lib/wizard-store";
import WizardCard from "../WizardCard";
import WizardControls from "../WizardControls";

export default function WelcomeStep() {
  const state = useWizardStore();
  const accepted = state.welcome.bsl_accepted;

  return (
    <WizardCard
      title="Welcome to MarvisX"
      description={
        <>
          AI-native project management OS. Your Company Brain — tasks, memory,
          and agents stay linked across sessions.
        </>
      }
      controls={
        <WizardControls
          canContinue={accepted}
          onContinue={() => {
            wizardActions.setWelcome({
              accepted_at: new Date().toISOString(),
            });
            wizardActions.advance();
          }}
        />
      }
    >
      <label className="flex items-start gap-3 text-sm">
        <input
          type="checkbox"
          checked={accepted}
          onChange={(e) =>
            wizardActions.setWelcome({ bsl_accepted: e.target.checked })
          }
          className="mt-1 h-4 w-4 rounded border border-pir bg-pir-base accent-pir-accent"
          aria-describedby="bsl-help"
        />
        <span className="text-pir-text-secondary">
          I accept the{" "}
          <a
            href="/LICENSE"
            target="_blank"
            rel="noreferrer noopener"
            className="text-pir-accent underline-offset-4 hover:underline"
          >
            Business Source License (BSL 1.1)
          </a>
          .
          <span
            id="bsl-help"
            className="mt-1 block text-xs text-pir-text-muted"
          >
            Source-available, converts to Apache 2.0 after four years.
          </span>
        </span>
      </label>
    </WizardCard>
  );
}
