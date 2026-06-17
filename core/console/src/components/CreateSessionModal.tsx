"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "next-themes";
import { createSession, getPrograms, getSessionCatalog } from "@/lib/api";
import type {
  ProjectInfo,
  SessionCatalogModel,
  SessionCatalogProvider,
  SessionCatalogResponse,
  SessionPermissionPreset,
  SessionProvider,
} from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";
import ProviderSelector from "./ProviderSelector";

interface CreateSessionModalProps {
  onClose: () => void;
  onCreated: (name: string, initialCommand?: string) => void;
}

const NAME_REGEX = /^[a-zA-Z0-9][a-zA-Z0-9\-]{0,28}[a-zA-Z0-9]$/;

function formatContext(contextWindow: number | null): string {
  if (!contextWindow) return "std";
  if (contextWindow >= 1_000_000) return "1M";
  if (contextWindow >= 1_000) return `${Math.round(contextWindow / 1000)}K`;
  return String(contextWindow);
}

function selectedLabel(value: string | null | undefined, fallback = "auto"): string {
  if (!value) return fallback;
  return value;
}

export default function CreateSessionModal({
  onClose,
  onCreated,
}: CreateSessionModalProps) {
  const { resolvedTheme } = useTheme();
  const [name, setName] = useState("");
  const [provider, setProvider] = useState<SessionProvider>("claude");
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [permissionPreset, setPermissionPreset] = useState<string>("");
  const [catalog, setCatalog] = useState<SessionCatalogResponse | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [projectQuery, setProjectQuery] = useState("");
  const [selectedProject, setSelectedProject] = useState<ProjectInfo | null>(null);
  const [showProjectList, setShowProjectList] = useState(false);
  const projectInputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const isValid = name.length >= 2 && NAME_REGEX.test(name);

  useEffect(() => {
    const ctrl = new AbortController();
    getSessionCatalog({ signal: ctrl.signal })
      .then((data) => {
        setCatalog(data);
        const claudeProvider = data.providers.find((entry) => entry.id === "claude");
        setSelectedModel(claudeProvider?.default_model || "");
        setPermissionPreset("");
      })
      .catch((err) => {
        setCatalogError(err instanceof Error ? err.message : "Failed to load session catalog");
      });

    getPrograms({ signal: ctrl.signal })
      .then((programs) => {
        const all: ProjectInfo[] = [];
        for (const program of programs) {
          for (const p of program.projects) {
            if (p.on_server && p.path) all.push(p);
          }
        }
        all.sort((a, b) => {
          const pa = a.program || "";
          const pb = b.program || "";
          if (pa !== pb) return pa.localeCompare(pb);
          return a.slug.localeCompare(b.slug);
        });
        setProjects(all);
      })
      .catch(() => {});

    return () => ctrl.abort();
  }, []);

  const providers = catalog?.providers ?? [];
  const providerConfig = useMemo<SessionCatalogProvider | null>(
    () => providers.find((entry) => entry.id === provider) ?? null,
    [providers, provider],
  );
  const modelConfig = useMemo<SessionCatalogModel | null>(
    () => providerConfig?.models.find((entry) => entry.id === selectedModel) ?? null,
    [providerConfig, selectedModel],
  );
  const permissionOptions = providerConfig?.permission_presets ?? [];
  const selectedPermission = useMemo<SessionPermissionPreset | null>(
    () => permissionOptions.find((entry) => entry.id === permissionPreset) ?? null,
    [permissionOptions, permissionPreset],
  );

  useEffect(() => {
    if (!providerConfig) return;
    const hasModel = providerConfig.models.some((entry) => entry.id === selectedModel);
    if (!hasModel) {
      setSelectedModel(providerConfig.default_model);
    }
    if (providerConfig.permission_presets.length > 0) {
      const hasPreset = providerConfig.permission_presets.some((entry) => entry.id === permissionPreset);
      if (!hasPreset) {
        setPermissionPreset(providerConfig.permission_presets[0].id);
      }
    } else if (permissionPreset) {
      setPermissionPreset("");
    }
  }, [permissionPreset, providerConfig, selectedModel]);

  const filtered = useMemo(() => {
    if (!projectQuery.trim()) return projects.slice(0, 8);
    const q = projectQuery.toLowerCase();
    const matches = projects.filter(
      (p) =>
        p.slug.toLowerCase().includes(q) ||
        (p.program && p.program.toLowerCase().includes(q)),
    );
    matches.sort((a, b) => {
      const aSlug = a.slug.toLowerCase().includes(q) ? 0 : 1;
      const bSlug = b.slug.toLowerCase().includes(q) ? 0 : 1;
      return aSlug - bSlug;
    });
    return matches.slice(0, 12);
  }, [projects, projectQuery]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!isValid || !providerConfig) return;

    setError("");
    setLoading(true);

    try {
      await createSession({
        name,
        project_slug: selectedProject?.slug || undefined,
        provider,
        model: selectedModel,
        permission_preset: permissionPreset || undefined,
        theme_mode: resolvedTheme === "light" ? "light" : "dark",
      });
      onCreated(name);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create session");
    } finally {
      setLoading(false);
    }
  }

  function selectProject(project: ProjectInfo) {
    setSelectedProject(project);
    setProjectQuery("");
    setShowProjectList(false);
  }

  function clearProject() {
    setSelectedProject(null);
    setProjectQuery("");
  }

  const launchRoot = "~/workspace";
  const bootState = "manual";
  const contextState = formatContext(modelConfig?.context_window ?? null);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-[2px]"
      onClick={onClose}
    >
      <form
        onSubmit={handleSubmit}
        className="flex max-h-[calc(100vh-32px)] w-[min(92vw,920px)] flex-col overflow-hidden rounded-2xl border border-pir bg-pir-surface-0 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-pir-text-primary">New Session</h2>
            <p className="mt-1 text-xs text-pir-text-muted">
              Launch in shared workspace first, then open project context manually.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-pir-text-muted transition-colors hover:text-pir-text-primary"
            aria-label="Close modal"
          >
            <svg className="h-4 w-4" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="2" y1="2" x2="12" y2="12" />
              <line x1="12" y1="2" x2="2" y2="12" />
            </svg>
          </button>
        </div>

        {(error || catalogError) && (
          <ErrorAlert message={error || catalogError} className="mb-3 text-sm" />
        )}

        <div className="grid min-h-0 gap-5 lg:grid-cols-[320px_minmax(0,1fr)]">
          <div className="space-y-4">
            <label className="mb-1 block font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">
              Session
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-session"
              className="mb-1 w-full rounded-lg border border-pir bg-pir-base px-3 py-2 text-sm font-mono text-pir-text-primary focus:border-pir-accent focus:outline-none"
              autoFocus
              maxLength={30}
              disabled={loading}
            />
            <p className="text-[11px] text-pir-text-muted">
              2-30 chars, letters or numbers, hyphens only.
            </p>

            <div>
              <label className="mb-1 block font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">
                Provider
              </label>
              <ProviderSelector
                value={provider}
                onChange={setProvider}
                providers={providers}
                disabled={loading || providers.length === 0}
              />
            </div>

            <div>
              <label className="mb-1 block font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">
                Project <span className="normal-case tracking-normal text-pir-text-muted">(optional metadata)</span>
              </label>
              {selectedProject ? (
                <div className="flex items-center gap-2 rounded-lg border border-pir-accent bg-[hsl(var(--pir-accent)/0.14)] px-3 py-2">
                  <span className="h-2 w-2 shrink-0 rounded-full bg-pir-accent" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-mono text-[13px] tabular-nums lowercase text-pir-text-primary">{selectedProject.slug}</div>
                    <div className="truncate font-mono text-[11px] tabular-nums lowercase text-pir-text-muted">
                      {selectedProject.program || "project"} · {selectedProject.path}
                    </div>
                    <div className="mt-1 truncate text-[11px] text-pir-text-muted">
                      Stored on the session card only. Launch stays in ~/workspace.
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={clearProject}
                    className="text-pir-text-muted transition-colors hover:text-pir-text-primary"
                    aria-label="Clear project"
                  >
                    <svg className="h-3.5 w-3.5" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2">
                      <line x1="2" y1="2" x2="12" y2="12" />
                      <line x1="12" y1="2" x2="2" y2="12" />
                    </svg>
                  </button>
                </div>
              ) : (
                <div className="relative">
                  <input
                    ref={projectInputRef}
                    type="text"
                    value={projectQuery}
                    onChange={(e) => {
                      setProjectQuery(e.target.value);
                      setShowProjectList(true);
                    }}
                    onFocus={() => setShowProjectList(true)}
                    onBlur={() => setTimeout(() => setShowProjectList(false), 150)}
                    placeholder="Search project to tag this session..."
                    className="w-full rounded-lg border border-pir bg-pir-base px-3 py-2 text-sm text-pir-text-primary placeholder:text-pir-text-muted focus:border-pir-accent focus:outline-none"
                    disabled={loading}
                  />
                  {showProjectList && filtered.length > 0 && (
                    <div
                      ref={listRef}
                      className="absolute left-0 right-0 top-full z-10 mt-1 max-h-48 overflow-y-auto rounded-lg border border-pir bg-pir-surface-1"
                    >
                      {filtered.map((project) => (
                        <button
                          key={project.slug}
                          type="button"
                          className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-pir-surface-2"
                          onMouseDown={() => selectProject(project)}
                        >
                          <span className="h-2 w-2 shrink-0 rounded-full bg-pir-text-muted" />
                          <div className="min-w-0 flex-1">
                            <div className="truncate font-mono text-[13px] tabular-nums lowercase text-pir-text-primary">{project.slug}</div>
                            <div className="truncate font-mono text-[11px] tabular-nums lowercase text-pir-text-muted">
                              {project.program || "project"} · {project.path}
                            </div>
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            <p className="text-[11px] text-pir-text-muted">
              MarvisX keeps startup in the shared workspace so AGENTS, CLAUDE, skills, hooks, and MCP config stay aligned.
            </p>

            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg border border-pir bg-pir-surface-1 px-3 py-2">
                <div className="font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">cwd</div>
                <div className="mt-1 truncate font-mono text-[11px] tabular-nums lowercase text-pir-text-secondary">{launchRoot}</div>
              </div>
              <div className="rounded-lg border border-pir bg-pir-surface-1 px-3 py-2">
                <div className="font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">ctx</div>
                <div className="mt-1 font-mono text-[11px] tabular-nums lowercase text-pir-text-secondary">{contextState}</div>
              </div>
              <div className="rounded-lg border border-pir bg-pir-surface-1 px-3 py-2">
                <div className="font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">boot</div>
                <div className="mt-1 truncate font-mono text-[11px] tabular-nums lowercase text-pir-text-secondary">{bootState}</div>
              </div>
              <div className="rounded-lg border border-pir bg-pir-surface-1 px-3 py-2">
                <div className="font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">perm</div>
                <div className="mt-1 truncate font-mono text-[11px] tabular-nums lowercase text-pir-text-secondary">
                  {selectedLabel(selectedPermission?.badge, "std")}
                </div>
              </div>
            </div>
          </div>

          <div className="min-h-0 space-y-4">
            <div>
              <div className="mb-1 flex items-center justify-between gap-3">
                <label className="block font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">
                  Model
                </label>
                {providerConfig?.note && (
                  <p className="hidden max-w-[320px] text-right text-[11px] text-pir-text-muted xl:block">
                    {providerConfig.note}
                  </p>
                )}
              </div>
              <div className="max-h-[440px] overflow-y-auto pr-1">
                <div className="grid gap-2 sm:grid-cols-2">
                  {(providerConfig?.models ?? []).map((model) => {
                    const active = model.id === selectedModel;
                    return (
                      <button
                        key={model.id}
                        type="button"
                        onClick={() => setSelectedModel(model.id)}
                        disabled={loading}
                        className={`h-full rounded-lg border px-3 py-2.5 text-left transition-colors ${
                          active
                            ? "border-pir-accent bg-[hsl(var(--pir-accent)/0.14)]"
                            : "border-pir bg-pir-surface-1 hover:border-pir-border-strong"
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-pir-text-primary">{model.label}</span>
                          {model.recommended && (
                            <span className="rounded border border-pir-accent/40 px-1.5 py-0.5 font-sans text-[9px] font-semibold uppercase tracking-[0.14em] text-pir-accent">
                              default
                            </span>
                          )}
                          {model.experimental && (
                            <span className="rounded border border-pir-warning/40 px-1.5 py-0.5 font-sans text-[9px] font-semibold uppercase tracking-[0.14em] text-pir-warning">
                              preview
                            </span>
                          )}
                          <span className="ml-auto rounded border border-pir px-1.5 py-0.5 font-mono text-[10px] tabular-nums lowercase text-pir-text-tertiary">
                            {formatContext(model.context_window)}
                          </span>
                        </div>
                        <div className="mt-1 text-[11px] leading-4 text-pir-text-secondary">{model.description}</div>
                        {model.note && (
                          <div className="mt-1 text-[10px] leading-4 text-pir-warning">{model.note}</div>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
              {providerConfig?.note && (
                <p className="mt-2 text-[11px] text-pir-text-muted xl:hidden">{providerConfig.note}</p>
              )}
            </div>

            {permissionOptions.length > 0 && (
              <div>
                <label className="mb-1 block font-sans text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">
                  Permissions
                </label>
                <div className="grid gap-2 sm:grid-cols-2">
                  {permissionOptions.map((preset) => {
                    const active = preset.id === permissionPreset;
                    return (
                      <button
                        key={preset.id}
                        type="button"
                        onClick={() => setPermissionPreset(preset.id)}
                        disabled={loading}
                        className={`rounded-lg border px-3 py-2 text-left transition-colors ${
                          active
                            ? "border-pir-accent bg-[hsl(var(--pir-accent)/0.14)]"
                            : "border-pir bg-pir-surface-1 hover:border-pir-border-strong"
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-pir-text-primary">{preset.label}</span>
                          <span className="ml-auto rounded border border-pir px-1.5 py-0.5 font-mono text-[10px] tabular-nums lowercase text-pir-text-tertiary">
                            {preset.badge}
                          </span>
                        </div>
                        <div className="mt-1 text-[11px] text-pir-text-secondary">{preset.description}</div>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="mt-5 flex items-center justify-end gap-3 border-t border-pir pt-4">
          <button
            type="button"
            onClick={onClose}
            className="rounded px-3 py-1.5 text-[13px] text-pir-text-secondary transition-colors hover:text-pir-text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-pir-accent/50"
            disabled={loading}
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || loading || !providerConfig}
            className="rounded bg-pir-accent px-4 py-2 text-[13px] font-medium text-pir-base transition-colors hover:bg-[hsl(var(--pir-accent)/0.88)] focus:outline-none focus-visible:ring-2 focus-visible:ring-pir-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Creating..." : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}
