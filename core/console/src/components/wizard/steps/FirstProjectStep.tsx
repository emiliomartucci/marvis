"use client";

import { useMemo, useState } from "react";
import { wizardActions, useWizardStore } from "@/lib/wizard-store";
import {
  slugify,
  validateFirstProject,
  type FirstProjectPayload,
  type ProjectType,
} from "@/lib/wizard-schemas";
import WizardCard from "../WizardCard";
import WizardControls from "../WizardControls";

const DEFAULTS: FirstProjectPayload = {
  name: "My first project",
  slug: "my-first-project",
  type: "code",
};

const TYPES: { value: ProjectType; label: string; description: string }[] = [
  {
    value: "code",
    label: "Code",
    description: "Git repo with worktree workflow",
  },
  {
    value: "work",
    label: "Work",
    description: "Metadata-only, no git",
  },
  {
    value: "system",
    label: "System",
    description: "Cross-project ops",
  },
];

export default function FirstProjectStep() {
  const state = useWizardStore();
  const [draft, setDraft] = useState<FirstProjectPayload>(
    state.first_project ?? DEFAULTS
  );
  const [slugTouched, setSlugTouched] = useState(false);

  const errors = useMemo(() => validateFirstProject(draft), [draft]);
  const errorByField = useMemo(
    () => Object.fromEntries(errors.map((e) => [e.field, e.message])),
    [errors]
  );

  return (
    <WizardCard
      title="Create your first project"
      description="A hello-world task is added so you have something to click on after finalize."
      controls={
        <WizardControls
          canContinue={errors.length === 0}
          onContinue={() => {
            wizardActions.setFirstProject(draft);
            wizardActions.advance();
          }}
        />
      }
    >
      <div>
        <label className="mb-1 block text-sm text-pir-text-secondary">
          Project name
        </label>
        <input
          type="text"
          value={draft.name}
          onChange={(e) => {
            const name = e.target.value;
            setDraft({
              ...draft,
              name,
              slug: slugTouched ? draft.slug : slugify(name),
            });
          }}
          className="w-full rounded border border-pir bg-pir-base px-3 py-2 text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
        />
        {errorByField.name ? (
          <p className="mt-1 text-xs text-red-500">{errorByField.name}</p>
        ) : null}
      </div>

      <div>
        <label className="mb-1 block text-sm text-pir-text-secondary">
          Slug
        </label>
        <input
          type="text"
          value={draft.slug}
          onChange={(e) => {
            setSlugTouched(true);
            setDraft({ ...draft, slug: e.target.value });
          }}
          className="w-full rounded border border-pir bg-pir-base px-3 py-2 font-mono text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
          spellCheck={false}
        />
        {errorByField.slug ? (
          <p className="mt-1 text-xs text-red-500">{errorByField.slug}</p>
        ) : null}
      </div>

      <fieldset>
        <legend className="mb-1 text-sm text-pir-text-secondary">
          Project type
        </legend>
        <div className="flex flex-col gap-2 text-sm">
          {TYPES.map((opt) => (
            <label
              key={opt.value}
              className="flex items-start gap-2 text-pir-text-secondary"
            >
              <input
                type="radio"
                name="project_type"
                value={opt.value}
                checked={draft.type === opt.value}
                onChange={() => setDraft({ ...draft, type: opt.value })}
                className="mt-1 accent-pir-accent"
              />
              <span>
                <span className="font-medium text-pir-text-primary">
                  {opt.label}
                </span>
                <span className="ml-2 text-xs text-pir-text-muted">
                  {opt.description}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
    </WizardCard>
  );
}
