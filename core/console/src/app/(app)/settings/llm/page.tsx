"use client";

import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { PageHeader } from "@/components/settings/PageHeader";
import { useT } from "@/lib/i18n";
import {
  APIError,
  createProviderKey,
  deleteProviderKey,
  getLlmConfig,
  getLlmStatus,
  listProviderKeys,
  putLlmConfig,
  type LlmConfigItem,
  type LlmConfigStatus,
  type LlmProvider,
  type ProviderKey,
} from "@/lib/api";

type LlmSettingsDictionary = ReturnType<typeof useT>["t"]["llmSettings"];

// Keyed providers require an api_key (the API returns 422 otherwise); the rest
// are keyless. Mirrors KEYED_PROVIDERS on the backend.
const PROVIDERS: LlmProvider[] = [
  "openai",
  "anthropic",
  "openai_compatible",
  "ollama",
  "mac_gateway",
];
const KEYED_PROVIDERS: ReadonlySet<LlmProvider> = new Set([
  "openai",
  "anthropic",
  "openai_compatible",
]);
const BASE_URL_PROVIDERS: ReadonlySet<LlmProvider> = new Set([
  "openai_compatible",
  "ollama",
]);

const PROVIDER_LABELS: Record<LlmProvider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  openai_compatible: "OpenAI-compatible",
  ollama: "Ollama",
  mac_gateway: "Mac Gateway",
};

function SectionCard({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="bg-pir-surface-1 border border-pir rounded-lg overflow-hidden">
      <div className="px-5 py-3 border-b border-pir">
        <h2 className="text-label text-pir-text-muted uppercase tracking-wider">{title}</h2>
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  );
}

function StatusPill({ status, t }: { status: LlmConfigStatus; t: LlmSettingsDictionary }) {
  const configured = status === "configured";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-caption font-semibold ${
        configured
          ? "bg-pir-success/15 text-pir-success"
          : "bg-pir-warning/10 text-pir-warning"
      }`}
    >
      <span
        aria-hidden
        className={`h-1.5 w-1.5 rounded-full ${configured ? "bg-pir-success" : "bg-pir-warning"}`}
      />
      {configured ? t.status.configured : t.status.disabled}
    </span>
  );
}

function fieldLabelClass(): string {
  return "text-label text-pir-text-secondary block mb-1.5";
}

function inputClass(): string {
  return "h-9 w-full rounded border border-pir bg-pir-surface-2 px-3 text-body text-pir-text-primary outline-none transition-colors placeholder:text-pir-text-muted focus:border-pir-accent";
}

export default function LlmSettingsPage() {
  const { t: dictionary } = useT();
  const t = dictionary.llmSettings;

  const [status, setStatus] = useState<LlmConfigStatus | null>(null);
  const [encryptionConfigured, setEncryptionConfigured] = useState(true);
  const [keys, setKeys] = useState<ProviderKey[]>([]);
  const [classify, setClassify] = useState<LlmConfigItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const reload = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setLoadError(false);
    try {
      const [statusRes, configRes, keysRes] = await Promise.all([
        getLlmStatus({ signal }),
        getLlmConfig({ signal }),
        listProviderKeys({ signal }),
      ]);
      setStatus(statusRes.classify);
      // Older backend without the field → undefined → treat as available, and
      // the submit-time 503 catch still surfaces the failure as a backstop.
      setEncryptionConfigured(statusRes.encryption_configured !== false);
      setKeys(keysRes);
      setClassify(configRes.find((item) => item.function_name === "classify") ?? null);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  return (
    <div className="h-full overflow-y-auto">
      <PageHeader eyebrow={t.eyebrow} title={t.title} sub={t.subtitle} />
      <div className="p-6 max-w-2xl space-y-6">
        {loading && (
          <p className="font-mono text-caption text-pir-text-muted">{t.loading}</p>
        )}

        {loadError && !loading && (
          <div className="rounded border border-pir-error/40 bg-pir-error/10 p-3 text-body text-pir-text-secondary">
            <p>{t.loadError}</p>
            <button
              type="button"
              className="mt-2 h-8 rounded border border-pir px-3 text-caption"
              onClick={() => reload()}
            >
              {t.retry}
            </button>
          </div>
        )}

        {!loading && !loadError && (
          <>
            <SectionCard title={t.status.title}>
              <div className="flex items-center justify-between gap-3">
                <p className="text-body text-pir-text-secondary">{t.runtimeNote}</p>
                {status && <StatusPill status={status} t={t} />}
              </div>
            </SectionCard>

            {!encryptionConfigured && (
              <div
                role="status"
                data-testid="byok-encryption-missing-notice"
                className="rounded border border-pir-warning/40 bg-pir-warning/10 p-3 text-body text-pir-text-secondary"
              >
                {t.addKey.encryptionMissing}
              </div>
            )}

            <ProviderKeysCard keys={keys} t={t} onChanged={() => reload()} />

            <AddKeyCard t={t} encryptionConfigured={encryptionConfigured} onAdded={() => reload()} />

            <ClassifyCard classify={classify} keys={keys} t={t} onSaved={() => reload()} />
          </>
        )}
      </div>
    </div>
  );
}

function ProviderKeysCard({
  keys,
  t,
  onChanged,
}: {
  keys: ProviderKey[];
  t: LlmSettingsDictionary;
  onChanged: () => void;
}) {
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleDelete(key: ProviderKey) {
    if (typeof window !== "undefined" && !window.confirm(t.keys.deleteConfirm)) return;
    setDeletingId(key.id);
    setError(null);
    try {
      await deleteProviderKey(key.id);
      onChanged();
    } catch {
      setError(t.addKey.genericError);
    } finally {
      setDeletingId(null);
    }
  }

  function keyStatusLabel(key: ProviderKey): string {
    if (key.key_status === "set") return t.keys.statusSet;
    if (key.key_status === "unreadable") return t.keys.statusUnreadable;
    return t.keys.statusNone;
  }

  return (
    <SectionCard title={t.keys.title}>
      {keys.length === 0 ? (
        <p className="text-body text-pir-text-muted">{t.keys.empty}</p>
      ) : (
        <div className="space-y-2">
          {keys.map((key) => (
            <div
              key={key.id}
              className="flex items-center justify-between gap-3 rounded border border-pir bg-pir-surface-2 px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-body text-pir-text-primary font-medium">
                    {PROVIDER_LABELS[key.provider as LlmProvider] ?? key.provider}
                  </span>
                  <span className="text-caption text-pir-text-muted truncate">
                    {key.label || t.keys.noLabel}
                  </span>
                </div>
                <div className="mt-0.5 flex items-center gap-3 text-caption text-pir-text-muted">
                  {key.key_prefix && (
                    <span className="font-mono">
                      {t.keys.prefix}: {key.key_prefix}
                    </span>
                  )}
                  <span>
                    {t.keys.statusLabel}: {keyStatusLabel(key)}
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => handleDelete(key)}
                disabled={deletingId === key.id}
                className="h-8 shrink-0 rounded border border-pir-error/50 px-3 text-caption text-pir-error transition-colors hover:bg-pir-error/10 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {deletingId === key.id ? t.keys.deleting : t.keys.delete}
              </button>
            </div>
          ))}
        </div>
      )}
      {error && (
        <p className="mt-3 text-caption text-pir-error" role="status">
          {error}
        </p>
      )}
    </SectionCard>
  );
}

function AddKeyCard({
  t,
  encryptionConfigured,
  onAdded,
}: {
  t: LlmSettingsDictionary;
  encryptionConfigured: boolean;
  onAdded: () => void;
}) {
  const [provider, setProvider] = useState<LlmProvider>("openai");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [label, setLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const needsKey = KEYED_PROVIDERS.has(provider);
  const showBaseUrl = BASE_URL_PROVIDERS.has(provider);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setMessage(null);
    setError(null);

    if (needsKey && !apiKey.trim()) {
      setError(t.addKey.apiKeyRequired);
      return;
    }

    setSubmitting(true);
    try {
      await createProviderKey({
        provider,
        api_key: apiKey.trim() || undefined,
        base_url: showBaseUrl && baseUrl.trim() ? baseUrl.trim() : undefined,
        label: label.trim() || undefined,
      });
      setMessage(t.addKey.success);
      setApiKey("");
      setBaseUrl("");
      setLabel("");
      onAdded();
    } catch (err) {
      // 503 = server encryption (BYOK_FERNET_SECRET) is not configured. Say so
      // honestly: the admin must set it and restart, no key can be stored yet.
      if (err instanceof APIError && err.status === 503) {
        setError(t.addKey.encryptionMissing);
      } else if (err instanceof APIError && err.status === 422) {
        setError(t.addKey.apiKeyRequired);
      } else {
        setError(t.addKey.genericError);
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <SectionCard title={t.addKey.title}>
      <form className="space-y-4" onSubmit={handleSubmit}>
        <div>
          <label className={fieldLabelClass()} htmlFor="llm-provider">
            {t.addKey.provider}
          </label>
          <select
            id="llm-provider"
            value={provider}
            onChange={(event) => setProvider(event.target.value as LlmProvider)}
            className={inputClass()}
          >
            {PROVIDERS.map((value) => (
              <option key={value} value={value}>
                {PROVIDER_LABELS[value]}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className={fieldLabelClass()} htmlFor="llm-api-key">
            {t.addKey.apiKey}
          </label>
          <input
            id="llm-api-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={t.addKey.apiKeyPlaceholder}
            className={inputClass()}
          />
          {!needsKey && (
            <p className="mt-1 text-caption text-pir-text-muted">{t.addKey.apiKeyOptional}</p>
          )}
        </div>

        {showBaseUrl && (
          <div>
            <label className={fieldLabelClass()} htmlFor="llm-base-url">
              {t.addKey.baseUrl}
            </label>
            <input
              id="llm-base-url"
              type="text"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder={t.addKey.baseUrlPlaceholder}
              className={inputClass()}
            />
          </div>
        )}

        <div>
          <label className={fieldLabelClass()} htmlFor="llm-label">
            {t.addKey.labelField}
          </label>
          <input
            id="llm-label"
            type="text"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder={t.addKey.labelPlaceholder}
            className={inputClass()}
          />
        </div>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={submitting || !encryptionConfigured}
            title={!encryptionConfigured ? t.addKey.encryptionMissing : undefined}
            className="h-9 rounded bg-pir-accent px-4 text-label font-semibold text-pir-base transition-colors hover:bg-pir-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? t.addKey.submitting : t.addKey.submit}
          </button>
          {message && (
            <span className="text-caption text-pir-success" role="status">
              {message}
            </span>
          )}
        </div>

        {error && (
          <div className="rounded border border-pir-warning/40 bg-pir-warning/10 p-3 text-body text-pir-text-secondary" role="status">
            {error}
          </div>
        )}
      </form>
    </SectionCard>
  );
}

function ClassifyCard({
  classify,
  keys,
  t,
  onSaved,
}: {
  classify: LlmConfigItem | null;
  keys: ProviderKey[];
  t: LlmSettingsDictionary;
  onSaved: () => void;
}) {
  const [providerKeyId, setProviderKeyId] = useState<string>(classify?.provider_key_id ?? "");
  const [model, setModel] = useState<string>(classify?.model ?? "");
  const [enabled, setEnabled] = useState<boolean>(classify?.enabled ?? false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Re-sync local form when the upstream config reloads (e.g. after save).
  useEffect(() => {
    setProviderKeyId(classify?.provider_key_id ?? "");
    setModel(classify?.model ?? "");
    setEnabled(classify?.enabled ?? false);
  }, [classify]);

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setMessage(null);
    setError(null);
    setSaving(true);
    try {
      await putLlmConfig("classify", {
        provider_key_id: providerKeyId || null,
        model: model.trim() || null,
        enabled,
      });
      setMessage(t.classify.saved);
      onSaved();
    } catch {
      setError(t.classify.saveError);
    } finally {
      setSaving(false);
    }
  }

  const noKeys = keys.length === 0;

  return (
    <SectionCard title={t.classify.title}>
      <p className="mb-4 text-body text-pir-text-secondary">{t.classify.explain}</p>
      <form className="space-y-4" onSubmit={handleSave}>
        <div>
          <label className={fieldLabelClass()} htmlFor="classify-provider-key">
            {t.classify.providerKey}
          </label>
          <select
            id="classify-provider-key"
            value={providerKeyId}
            onChange={(event) => setProviderKeyId(event.target.value)}
            disabled={noKeys}
            className={`${inputClass()} disabled:cursor-not-allowed disabled:opacity-50`}
          >
            <option value="">{t.classify.providerKeyNone}</option>
            {keys.map((key) => (
              <option key={key.id} value={key.id}>
                {(PROVIDER_LABELS[key.provider as LlmProvider] ?? key.provider)}
                {key.label ? ` — ${key.label}` : ""}
              </option>
            ))}
          </select>
          {noKeys && (
            <p className="mt-1 text-caption text-pir-text-muted">{t.classify.noKeysHint}</p>
          )}
        </div>

        <div>
          <label className={fieldLabelClass()} htmlFor="classify-model">
            {t.classify.model}
          </label>
          <input
            id="classify-model"
            type="text"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder={t.classify.modelPlaceholder}
            className={inputClass()}
          />
        </div>

        <label className="flex items-center gap-2 text-body text-pir-text-secondary">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            className="h-4 w-4 accent-pir-accent"
          />
          {t.classify.enabled}
        </label>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={saving}
            className="h-9 rounded bg-pir-accent px-4 text-label font-semibold text-pir-base transition-colors hover:bg-pir-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? t.classify.saving : t.classify.save}
          </button>
          {message && (
            <span className="text-caption text-pir-success" role="status">
              {message}
            </span>
          )}
          {error && (
            <span className="text-caption text-pir-error" role="status">
              {error}
            </span>
          )}
        </div>
      </form>
    </SectionCard>
  );
}
