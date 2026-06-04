"use client";

import { useMemo, useState } from "react";
import { wizardActions, useWizardStore } from "@/lib/wizard-store";
import {
  validateStorage,
  type StoragePayload,
} from "@/lib/wizard-schemas";
import WizardCard from "../WizardCard";
import WizardControls from "../WizardControls";

const DEFAULTS: StoragePayload = {
  projects_root: "/data/projects",
  db_backend: "sqlite",
  db_path: "/data/marvisx/db/console.db",
  postgres_dsn: null,
};

export default function StorageStep() {
  const state = useWizardStore();
  const [draft, setDraft] = useState<StoragePayload>(
    state.storage ?? DEFAULTS
  );

  const errors = useMemo(() => validateStorage(draft), [draft]);
  const errorByField = useMemo(
    () => Object.fromEntries(errors.map((e) => [e.field, e.message])),
    [errors]
  );

  return (
    <WizardCard
      title="Where should MarvisX store data?"
      description={
        <>Where project metadata, handoffs, and the local database live.</>
      }
      controls={
        <WizardControls
          canContinue={errors.length === 0}
          onContinue={() => {
            wizardActions.setStorage(draft);
            wizardActions.advance();
          }}
        />
      }
    >
      <div>
        <label className="mb-1 block text-sm text-pir-text-secondary">
          Projects root
        </label>
        <input
          type="text"
          value={draft.projects_root}
          onChange={(e) =>
            setDraft({ ...draft, projects_root: e.target.value })
          }
          className="w-full rounded border border-pir bg-pir-base px-3 py-2 text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
          spellCheck={false}
        />
        {errorByField.projects_root ? (
          <p className="mt-1 text-xs text-red-500">
            {errorByField.projects_root}
          </p>
        ) : null}
      </div>

      <fieldset>
        <legend className="mb-1 text-sm text-pir-text-secondary">
          Database backend
        </legend>
        <div className="flex flex-col gap-2 text-sm">
          <label className="flex items-center gap-2 text-pir-text-secondary">
            <input
              type="radio"
              name="db_backend"
              value="sqlite"
              checked={draft.db_backend === "sqlite"}
              onChange={() => setDraft({ ...draft, db_backend: "sqlite" })}
              className="accent-pir-accent"
            />
            SQLite (recommended for single-user)
          </label>
          <label className="flex items-center gap-2 text-pir-text-secondary">
            <input
              type="radio"
              name="db_backend"
              value="postgres"
              checked={draft.db_backend === "postgres"}
              onChange={() =>
                setDraft({ ...draft, db_backend: "postgres" })
              }
              className="accent-pir-accent"
            />
            Postgres
          </label>
        </div>
      </fieldset>

      {draft.db_backend === "sqlite" ? (
        <div>
          <label className="mb-1 block text-sm text-pir-text-secondary">
            Database path
          </label>
          <input
            type="text"
            value={draft.db_path ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, db_path: e.target.value })
            }
            className="w-full rounded border border-pir bg-pir-base px-3 py-2 text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
            spellCheck={false}
          />
          {errorByField.db_path ? (
            <p className="mt-1 text-xs text-red-500">{errorByField.db_path}</p>
          ) : null}
        </div>
      ) : (
        <div>
          <label className="mb-1 block text-sm text-pir-text-secondary">
            Postgres DSN
          </label>
          <input
            type="text"
            placeholder="postgresql://user:password@localhost:5432/db"
            value={draft.postgres_dsn ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, postgres_dsn: e.target.value })
            }
            className="w-full rounded border border-pir bg-pir-base px-3 py-2 text-sm text-pir-text-primary focus:border-pir-accent focus:outline-none"
            spellCheck={false}
          />
          {errorByField.postgres_dsn ? (
            <p className="mt-1 text-xs text-red-500">
              {errorByField.postgres_dsn}
            </p>
          ) : null}
          <p className="mt-1 text-xs text-pir-text-muted">
            DSN smoke test runs at finalize (Wave 2).
          </p>
        </div>
      )}
    </WizardCard>
  );
}
