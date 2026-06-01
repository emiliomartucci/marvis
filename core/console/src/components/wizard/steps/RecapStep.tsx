"use client";

import { wizardActions, useWizardStore } from "@/lib/wizard-store";
import { maskApiKey } from "@/lib/wizard-schemas";
import WizardCard from "../WizardCard";
import WizardControls from "../WizardControls";

function RecapRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-pir py-2 last:border-b-0">
      <span className="text-sm text-pir-text-muted">{label}</span>
      <span className="text-right font-mono text-sm text-pir-text-primary">
        {value}
      </span>
    </div>
  );
}

export default function RecapStep() {
  const state = useWizardStore();

  return (
    <WizardCard
      title="Review and finalize"
      description="Confirm your choices below. Finalize writes settings.yaml + byok.vault + project.yaml in a single atomic transaction (Wave 2)."
      controls={
        <WizardControls
          canContinue
          continueLabel="Finalize and enter"
          onContinue={() => {
            wizardActions.finalize();
            // Wave 2 will POST /api/v1/wizard/finalize + redirect /tasks.
          }}
        />
      }
    >
      <div>
        <RecapRow
          label="License"
          value={
            state.welcome.bsl_accepted ? "BSL 1.1 accepted" : "Not accepted"
          }
        />
        <RecapRow
          label="Projects root"
          value={state.storage?.projects_root ?? "—"}
        />
        <RecapRow
          label="Database"
          value={
            state.storage
              ? state.storage.db_backend === "sqlite"
                ? `SQLite → ${state.storage.db_path ?? "—"}`
                : `Postgres → ${state.storage.postgres_dsn ?? "—"}`
              : "—"
          }
        />
        <RecapRow
          label="LLM"
          value={
            state.llm_provider?.provider
              ? `${state.llm_provider.provider} (${maskApiKey(
                  state.llm_provider.api_key,
                )})`
              : "Configure later"
          }
        />
        <RecapRow
          label="First project"
          value={
            state.first_project
              ? `${state.first_project.slug} (${state.first_project.type})`
              : "Configure later"
          }
        />
      </div>

      <p className="text-xs text-pir-text-muted">
        Activation: a hello-world task is created in your first project, so the
        Console lands non-empty.
      </p>

      {state.completed_at ? (
        <p className="text-xs text-pir-accent">
          Wizard finalized at{" "}
          {new Date(state.completed_at).toLocaleString()}.
        </p>
      ) : null}
    </WizardCard>
  );
}
