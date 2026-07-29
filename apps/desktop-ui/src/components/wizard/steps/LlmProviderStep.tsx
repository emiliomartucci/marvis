"use client";

import { useMemo, useState } from "react";
import { wizardActions, useWizardStore } from "@/lib/wizard-store";
import {
  validateLlmProvider,
  type LlmProvider,
  type LlmProviderPayload,
} from "@/lib/wizard-schemas";
import WizardCard from "../WizardCard";
import WizardControls from "../WizardControls";

// `mac_gateway` is a personal setup, not an OSS onboarding option (gh #32) —
// not offered here. The schema enum keeps it for backend compatibility.
const PROVIDERS: { value: LlmProvider; label: string }[] = [
  { value: "anthropic", label: "Anthropic" },
  { value: "openai", label: "OpenAI" },
  { value: "bedrock", label: "AWS Bedrock" },
];

const EMPTY: LlmProviderPayload = {
  provider: null,
  api_key: null,
  base_url: null,
  test_passed: false,
};

export default function LlmProviderStep() {
  const state = useWizardStore();
  const [draft, setDraft] = useState<LlmProviderPayload>(
    state.llm_provider ?? EMPTY
  );
  const [showKey, setShowKey] = useState(false);

  const errors = useMemo(
    () => validateLlmProvider(draft, { allowEmpty: false }),
    [draft]
  );
  const errorByField = useMemo(
    () => Object.fromEntries(errors.map((e) => [e.field, e.message])),
    [errors]
  );

  return (
    <WizardCard
      title="Bring your LLM provider"
      description={
        <>
          Your keys stay local — encrypted in{" "}
          <code className="rounded bg-pir-base px-1 py-0.5 text-xs">
            ~/.marvis/byok.vault
          </code>
          . The master key is generated at first run and never sent to us.
        </>
      }
      controls={
        <WizardControls
          canContinue={errors.length === 0}
          onContinue={() => {
            wizardActions.setLlmProvider(draft);
            wizardActions.advance();
          }}
        />
      }
    >
      <div>
        <label className="mb-1 block text-sm text-pir-text-secondary">
          Provider
        </label>
        <select
          value={draft.provider ?? ""}
          onChange={(e) =>
            setDraft({
              ...draft,
              provider: (e.target.value || null) as LlmProvider | null,
            })
          }
          className="w-full rounded border border-pir bg-pir-base px-3 py-2 text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
        >
          <option value="">Select a provider</option>
          {PROVIDERS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
        {errorByField.provider ? (
          <p className="mt-1 text-xs text-red-500">{errorByField.provider}</p>
        ) : null}
      </div>

      {draft.provider === "mac_gateway" ? (
        <div>
          <label className="mb-1 block text-sm text-pir-text-secondary">
            Gateway base URL
          </label>
          <input
            type="text"
            placeholder="http://localhost:4000"
            value={draft.base_url ?? ""}
            onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            className="w-full rounded border border-pir bg-pir-base px-3 py-2 text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
            spellCheck={false}
          />
          {errorByField.base_url ? (
            <p className="mt-1 text-xs text-red-500">
              {errorByField.base_url}
            </p>
          ) : null}
        </div>
      ) : null}

      <div>
        <div className="mb-1 flex items-center justify-between">
          <label className="text-sm text-pir-text-secondary">
            {draft.provider === "mac_gateway" ? "Virtual key" : "API key"}
          </label>
          <button
            type="button"
            onClick={() => setShowKey((v) => !v)}
            className="text-xs text-pir-text-muted hover:text-pir-text-secondary"
          >
            {showKey ? "Hide" : "Show"}
          </button>
        </div>
        <input
          type={showKey ? "text" : "password"}
          autoComplete="off"
          value={draft.api_key ?? ""}
          onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
          className="w-full rounded border border-pir bg-pir-base px-3 py-2 font-mono text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
          spellCheck={false}
        />
        {errorByField.api_key ? (
          <p className="mt-1 text-xs text-red-500">{errorByField.api_key}</p>
        ) : null}
      </div>

      <p className="text-xs text-pir-text-muted">
        Test connection wires in Wave 2 — calls{" "}
        <code className="rounded bg-pir-base px-1 py-0.5 text-xs">
          POST /api/v1/llm/test-key
        </code>{" "}
        with a single-token probe.
      </p>
    </WizardCard>
  );
}
