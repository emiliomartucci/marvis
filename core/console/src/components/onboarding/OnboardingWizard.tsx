"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";

import {
  readOnboardingSetup,
  scanOnboardingWorkdir,
  seedOnboardingDemo,
  writeOnboardingSetup,
  type OnboardingScanCandidate,
} from "@/lib/api";
import {
  brainSourcesSetupContent,
  identitySetupContent,
  parseExclusions,
  rhythmSetupContent,
  sourcesSetupContent,
} from "@/lib/onboarding";
import { useT } from "@/lib/i18n";
import { ThemeToggle } from "@/components/ui/ThemeToggle";

type WizardStep = "how" | "identity" | "folder" | "brain" | "ready";

interface OnboardingWizardProps {
  onComplete: (options?: { startTour?: boolean; demoSeeded?: boolean }) => void;
}

const STEPS: WizardStep[] = ["how", "identity", "folder", "brain", "ready"];

function fmt(template: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce(
    (text, [key, value]) => text.replaceAll(`{${key}}`, String(value)),
    template,
  );
}

function sourceCount(docs: boolean, repos: boolean): number {
  return Number(docs) + Number(repos);
}

function selectedSources(
  proposals: OnboardingScanCandidate[],
  selected: Record<string, boolean>,
): string[] {
  return proposals.filter((candidate) => selected[candidate.path]).map((candidate) => candidate.path);
}

function WizardShell({
  step,
  busy,
  message,
  onSkipAll,
  children,
}: {
  step: WizardStep;
  busy: boolean;
  message: string | null;
  onSkipAll: () => void;
  children: ReactNode;
}) {
  const { t } = useT();
  const tt = t.onboarding;
  const currentIndex = STEPS.indexOf(step);

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-[110] flex bg-pir-base text-pir-text-primary"
    >
      <aside className="hidden w-[260px] shrink-0 border-r border-pir bg-pir-surface-0 p-5 md:flex md:flex-col">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="font-mono text-caption uppercase text-pir-accent">Marvis</p>
            <h2 className="mt-1 text-heading text-pir-text-primary">Setup</h2>
          </div>
          <ThemeToggle />
        </div>

        <ol className="mt-8 flex flex-col gap-2">
          {STEPS.map((item, index) => {
            const active = item === step;
            const done = index < currentIndex;
            return (
              <li
                key={item}
                className={`rounded border px-3 py-2 ${
                  active
                    ? "border-pir-accent bg-pir-accent/10 text-pir-text-primary"
                    : done
                      ? "border-pir-success/40 bg-pir-success/10 text-pir-text-secondary"
                      : "border-pir bg-pir-base text-pir-text-muted"
                }`}
              >
                <span className="font-mono text-[10px]">{String(index + 1).padStart(2, "0")}</span>
                <span className="ml-2 text-label">{tt.steps[item]}</span>
              </li>
            );
          })}
        </ol>

        <button
          type="button"
          onClick={onSkipAll}
          disabled={busy}
          className="mt-auto h-9 rounded border border-pir px-3 text-caption text-pir-text-tertiary hover:border-pir-accent hover:text-pir-text-primary disabled:opacity-60"
        >
          {tt.nav.skipAll}
        </button>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-pir bg-pir-surface-0 px-4 md:hidden">
          <span className="font-mono text-caption text-pir-text-muted">
            {fmt(tt.stepCounter, { current: currentIndex + 1, total: STEPS.length })}
          </span>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <button
              type="button"
              onClick={onSkipAll}
              className="h-8 rounded border border-pir px-2 text-caption text-pir-text-tertiary"
            >
              {tt.nav.close}
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 md:px-8 md:py-8">
          <div className="mx-auto flex min-h-full w-full max-w-5xl flex-col">
            {children}
            {message && (
              <p role="status" className="mt-4 font-mono text-caption text-pir-accent">
                {message}
              </p>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

function StepHeading({
  step,
  title,
  subtitle,
}: {
  step: WizardStep;
  title: string;
  subtitle: string;
}) {
  const { t } = useT();
  const index = STEPS.indexOf(step) + 1;
  return (
    <header className="mb-6 max-w-3xl">
      <p className="font-mono text-caption uppercase text-pir-accent">
        {fmt(t.onboarding.stepCounter, { current: index, total: STEPS.length })}
      </p>
      <h1 className="mt-2 text-display text-pir-text-primary">{title}</h1>
      <p className="mt-2 text-body leading-6 text-pir-text-secondary">{subtitle}</p>
    </header>
  );
}

function HowStep() {
  const { t } = useT();
  const tt = t.onboarding.how;
  const cards = [
    tt.cards.gate,
    tt.cards.agents,
    tt.cards.brain,
    tt.cards.audit,
  ];
  return (
    <>
      <StepHeading step="how" title={tt.title} subtitle={tt.subtitle} />
      <div className="grid gap-3 md:grid-cols-4">
        {cards.map((card, index) => (
          <article
            key={card.title}
            className="rounded border border-pir bg-pir-surface-0 p-4"
          >
            <span className="font-mono text-[10px] text-pir-accent">
              {String(index + 1).padStart(2, "0")}
            </span>
            <h2 className="mt-3 text-label font-semibold text-pir-text-primary">{card.title}</h2>
            <p className="mt-2 text-body leading-6 text-pir-text-secondary">{card.body}</p>
          </article>
        ))}
      </div>
      <div className="mt-4 rounded border border-pir-success/40 bg-pir-success/10 px-4 py-3 text-body text-pir-text-secondary">
        {tt.localNote}
      </div>
    </>
  );
}

function PromptBlock({
  root,
  sources,
  exclusions,
}: {
  root: string;
  sources: string[];
  exclusions: string[];
}) {
  const { t } = useT();
  const tt = t.onboarding.folder;
  const [copied, setCopied] = useState(false);
  const prompt = useMemo(() => {
    const sourceLines = sources.length ? sources.map((source) => `- ${source}`) : [tt.promptEmpty];
    const exclusionLines = exclusions.length ? exclusions.map((item) => `- ${item}`) : [tt.promptEmpty];
    return [
      "marvis init",
      "",
      tt.promptReadSetup,
      `${tt.promptRoot}: ${root || "-"}`,
      `${tt.promptSources}:`,
      ...sourceLines,
      `${tt.promptExclusions}:`,
      ...exclusionLines,
      tt.promptDerive,
    ].join("\n");
  }, [exclusions, root, sources, tt]);

  async function copyPrompt() {
    await navigator.clipboard?.writeText(prompt);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <section className="rounded border border-pir bg-pir-surface-0">
      <div className="flex items-center justify-between gap-3 border-b border-pir px-3 py-2">
        <div>
          <h3 className="text-label font-semibold text-pir-text-primary">{tt.promptTitle}</h3>
          <p className="mt-1 text-caption text-pir-text-muted">{tt.promptHint}</p>
        </div>
        <button
          type="button"
          onClick={copyPrompt}
          className="h-8 rounded border border-pir px-3 text-caption text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary"
        >
          {copied ? t.onboarding.nav.copied : t.onboarding.nav.copy}
        </button>
      </div>
      <pre className="max-h-56 overflow-auto whitespace-pre-wrap p-3 font-mono text-[11px] leading-5 text-pir-text-secondary">
        {prompt}
      </pre>
    </section>
  );
}

export function OnboardingWizard({ onComplete }: OnboardingWizardProps) {
  const { t, locale } = useT();
  const tt = t.onboarding;
  const [step, setStep] = useState<WizardStep>("how");
  const [operator, setOperator] = useState("");
  const [company, setCompany] = useState("");
  const [root, setRoot] = useState("");
  const [exclusionsInput, setExclusionsInput] = useState("");
  const [proposals, setProposals] = useState<OnboardingScanCandidate[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [sourcesConfirmed, setSourcesConfirmed] = useState(false);
  const [docsConsent, setDocsConsent] = useState(true);
  const [repoConsent, setRepoConsent] = useState(true);
  const [cycleHour, setCycleHour] = useState("03:00");
  const [setupPreview, setSetupPreview] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const exclusions = useMemo(() => parseExclusions(exclusionsInput), [exclusionsInput]);
  const sources = useMemo(() => selectedSources(proposals, selected), [proposals, selected]);
  const currentIndex = STEPS.indexOf(step);
  const isLast = step === "ready";

  useEffect(() => {
    if (step !== "ready") return;
    let stopped = false;
    const load = () => {
      readOnboardingSetup()
        .then((setup) => {
          if (!stopped) setSetupPreview(setup.content);
        })
        .catch(() => {
          if (!stopped) setSetupPreview("");
        });
    };
    load();
    const timer = window.setInterval(load, 5000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [step]);

  function draft() {
    return {
      operator,
      company,
      root,
      sources,
      exclusions,
      docsConsent,
      repoConsent,
      cycleHour,
    };
  }

  async function saveIdentity() {
    await writeOnboardingSetup({
      section: "Identità",
      content: identitySetupContent(draft()),
      checkboxes: { "Identità": true },
    });
  }

  async function saveSources() {
    await writeOnboardingSetup({
      section: "Sorgenti",
      content: sourcesSetupContent(draft()),
      checkboxes: { "Sorgenti": true },
    });
  }

  async function saveBrain() {
    const current = draft();
    await writeOnboardingSetup({
      section: "Ritmo",
      content: rhythmSetupContent(current),
      checkboxes: { "Ritmo": true },
    });
    await writeOnboardingSetup({
      section: "Fonti del brain",
      content: brainSourcesSetupContent(current),
      checkboxes: { "Fonti del brain": true },
    });
  }

  async function handleScan() {
    const trimmedRoot = root.trim();
    if (!trimmedRoot) return;
    setBusy(true);
    setMessage(tt.status.scanning);
    try {
      const response = await scanOnboardingWorkdir({ root: trimmedRoot, exclusions });
      setRoot(response.root);
      setProposals(response.proposals);
      setSelected(
        response.proposals.reduce<Record<string, boolean>>((acc, candidate) => {
          acc[candidate.path] = true;
          return acc;
        }, {}),
      );
      setSourcesConfirmed(false);
      setMessage(null);
    } catch {
      setMessage(tt.status.scanError);
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirmSources() {
    setBusy(true);
    setMessage(tt.status.saving);
    try {
      await saveSources();
      setSourcesConfirmed(true);
      setMessage(tt.status.saved);
    } catch {
      setMessage(tt.status.error);
    } finally {
      setBusy(false);
    }
  }

  async function persistCurrentStep() {
    if (step === "identity") await saveIdentity();
    if (step === "brain") await saveBrain();
  }

  async function goNext() {
    if (isLast) return;
    setBusy(true);
    setMessage(step === "how" || step === "folder" ? null : tt.status.saving);
    try {
      await persistCurrentStep();
      setStep(STEPS[currentIndex + 1]);
      setMessage(step === "how" || step === "folder" ? null : tt.status.saved);
    } catch {
      setMessage(tt.status.error);
    } finally {
      setBusy(false);
    }
  }

  function goBack() {
    if (currentIndex === 0 || busy) return;
    setStep(STEPS[currentIndex - 1]);
    setMessage(null);
  }

  function skipStep() {
    if (isLast) {
      onComplete();
      return;
    }
    setStep(STEPS[currentIndex + 1]);
    setMessage(null);
  }

  async function startTour() {
    setBusy(true);
    setMessage(tt.status.demoSeeding);
    try {
      await seedOnboardingDemo(locale);
      onComplete({ startTour: true, demoSeeded: true });
    } catch {
      setMessage(tt.status.demoError);
      setBusy(false);
    }
  }

  let body: React.ReactNode;
  if (step === "how") {
    body = <HowStep />;
  } else if (step === "identity") {
    body = (
      <>
        <StepHeading step="identity" title={tt.identity.title} subtitle={tt.identity.subtitle} />
        <div className="grid max-w-4xl gap-4 md:grid-cols-[minmax(0,1fr)_280px]">
          <div className="flex flex-col gap-4">
            <label className="block">
              <span className="font-mono text-caption uppercase text-pir-text-muted">
                {tt.identity.operatorLabel}
              </span>
              <input
                value={operator}
                onChange={(event) => setOperator(event.target.value)}
                placeholder={tt.identity.operatorPlaceholder}
                className="mt-2 h-10 w-full rounded border border-pir bg-pir-surface-0 px-3 text-body text-pir-text-primary outline-none focus:border-pir-accent"
              />
            </label>
            <label className="block">
              <span className="font-mono text-caption uppercase text-pir-text-muted">
                {tt.identity.companyLabel}
              </span>
              <input
                value={company}
                onChange={(event) => setCompany(event.target.value)}
                placeholder={tt.identity.companyPlaceholder}
                className="mt-2 h-10 w-full rounded border border-pir bg-pir-surface-0 px-3 text-body text-pir-text-primary outline-none focus:border-pir-accent"
              />
            </label>
          </div>
          <aside className="rounded border border-pir-accent/40 bg-pir-accent/10 p-4">
            <h2 className="text-label font-semibold text-pir-text-primary">
              {tt.identity.demoNoticeTitle}
            </h2>
            <p className="mt-2 text-body leading-6 text-pir-text-secondary">
              {tt.identity.demoNoticeBody}
            </p>
          </aside>
        </div>
      </>
    );
  } else if (step === "folder") {
    body = (
      <>
        <StepHeading step="folder" title={tt.folder.title} subtitle={tt.folder.subtitle} />
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)]">
          <section className="flex flex-col gap-4">
            <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(220px,0.65fr)]">
              <label className="block">
                <span className="font-mono text-caption uppercase text-pir-text-muted">
                  {tt.folder.rootLabel}
                </span>
                <input
                  value={root}
                  onChange={(event) => {
                    setRoot(event.target.value);
                    setSourcesConfirmed(false);
                  }}
                  placeholder={tt.folder.rootPlaceholder}
                  className="mt-2 h-10 w-full rounded border border-pir bg-pir-surface-0 px-3 font-mono text-body text-pir-text-primary outline-none focus:border-pir-accent"
                />
              </label>
              <label className="block">
                <span className="font-mono text-caption uppercase text-pir-text-muted">
                  {tt.folder.exclusionsLabel}
                </span>
                <input
                  value={exclusionsInput}
                  onChange={(event) => {
                    setExclusionsInput(event.target.value);
                    setSourcesConfirmed(false);
                  }}
                  placeholder={tt.folder.exclusionsPlaceholder}
                  className="mt-2 h-10 w-full rounded border border-pir bg-pir-surface-0 px-3 font-mono text-body text-pir-text-primary outline-none focus:border-pir-accent"
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={handleScan}
                disabled={!root.trim() || busy}
                className="h-9 rounded bg-pir-accent px-3 text-label font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-60"
              >
                {busy ? tt.status.scanning : proposals.length ? tt.folder.rescan : tt.folder.scan}
              </button>
              {proposals.length > 0 && (
                <>
                  <span className="font-mono text-caption text-pir-text-muted">
                    {fmt(tt.folder.found, { count: proposals.length })}
                  </span>
                  <span className="font-mono text-caption text-pir-text-muted">
                    {fmt(tt.folder.selected, { count: sources.length })}
                  </span>
                </>
              )}
            </div>

            {proposals.length > 0 && (
              <div className="flex max-h-[360px] flex-col gap-2 overflow-auto rounded border border-pir bg-pir-surface-0 p-2">
                {proposals.map((candidate) => {
                  const active = selected[candidate.path] !== false;
                  return (
                    <button
                      key={candidate.path}
                      type="button"
                      onClick={() => {
                        setSelected((current) => ({ ...current, [candidate.path]: !active }));
                        setSourcesConfirmed(false);
                      }}
                      className={`flex items-start gap-3 rounded border px-3 py-2 text-left transition-colors ${
                        active
                          ? "border-pir-accent bg-pir-accent/10"
                          : "border-pir bg-pir-base text-pir-text-muted"
                      }`}
                    >
                      <span
                        className={`mt-0.5 h-4 w-4 shrink-0 rounded border ${
                          active ? "border-pir-accent bg-pir-accent" : "border-pir"
                        }`}
                        aria-hidden
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-mono text-caption text-pir-text-primary">
                          {candidate.path}
                        </span>
                        <span className="mt-1 block text-caption text-pir-text-muted">
                          {candidate.name}
                        </span>
                      </span>
                      <span className="rounded border border-pir bg-pir-surface-1 px-1.5 py-0.5 font-mono text-[10px] text-pir-text-muted">
                        {candidate.kind === "code" ? tt.folder.code : tt.folder.noCode}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}

            {proposals.length > 0 && (
              <button
                type="button"
                onClick={handleConfirmSources}
                disabled={sources.length === 0 || busy}
                className="h-9 w-fit rounded border border-pir-accent bg-pir-accent/10 px-3 text-label font-semibold text-pir-accent hover:bg-pir-accent/20 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {sourcesConfirmed ? tt.folder.confirmed : tt.folder.confirm}
              </button>
            )}
          </section>
          <PromptBlock root={root} sources={sources} exclusions={exclusions} />
        </div>
      </>
    );
  } else if (step === "brain") {
    body = (
      <>
        <StepHeading step="brain" title={tt.brain.title} subtitle={tt.brain.subtitle} />
        <div className="grid max-w-4xl gap-4 md:grid-cols-[minmax(0,1fr)_260px]">
          <section className="rounded border border-pir bg-pir-surface-0 p-4">
            <h2 className="text-label font-semibold text-pir-text-primary">{tt.brain.sourcesTitle}</h2>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {[
                { id: "docs", label: tt.brain.docs, hint: tt.brain.docsHint, checked: docsConsent, set: setDocsConsent, disabled: false },
                { id: "repos", label: tt.brain.repos, hint: tt.brain.reposHint, checked: repoConsent, set: setRepoConsent, disabled: false },
                { id: "email", label: tt.brain.email, hint: tt.brain.enterpriseSoon, checked: false, set: null, disabled: true },
                { id: "kb", label: tt.brain.kb, hint: tt.brain.enterpriseSoon, checked: false, set: null, disabled: true },
              ].map((source) => (
                <button
                  key={source.id}
                  type="button"
                  disabled={source.disabled}
                  onClick={() => source.set?.(!source.checked)}
                  className={`rounded border p-3 text-left transition-colors ${
                    source.checked
                      ? "border-pir-accent bg-pir-accent/10"
                      : "border-pir bg-pir-base"
                  } ${source.disabled ? "cursor-not-allowed opacity-55" : "hover:border-pir-accent"}`}
                >
                  <span className="flex items-center justify-between gap-3">
                    <span className="text-label font-semibold text-pir-text-primary">{source.label}</span>
                    <span
                      className={`h-5 w-9 rounded-full border p-0.5 ${
                        source.checked ? "border-pir-accent bg-pir-accent" : "border-pir bg-pir-surface-2"
                      }`}
                      aria-hidden
                    >
                      <span
                        className={`block h-3.5 w-3.5 rounded-full bg-pir-base transition-transform ${
                          source.checked ? "translate-x-4" : ""
                        }`}
                      />
                    </span>
                  </span>
                  <span className="mt-2 block text-caption leading-5 text-pir-text-muted">
                    {source.hint}
                  </span>
                </button>
              ))}
            </div>
          </section>
          <label className="rounded border border-pir bg-pir-surface-0 p-4">
            <span className="font-mono text-caption uppercase text-pir-text-muted">
              {tt.brain.cycleLabel}
            </span>
            <input
              value={cycleHour}
              onChange={(event) => setCycleHour(event.target.value)}
              type="time"
              className="mt-3 h-10 w-full rounded border border-pir bg-pir-base px-3 font-mono text-body text-pir-text-primary outline-none focus:border-pir-accent"
            />
            <span className="mt-3 block text-body leading-6 text-pir-text-secondary">
              {tt.brain.cycleHint}
            </span>
          </label>
        </div>
      </>
    );
  } else {
    body = (
      <>
        <StepHeading step="ready" title={tt.ready.title} subtitle={tt.ready.subtitle} />
        <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(360px,1.1fr)]">
          <section className="flex flex-col gap-3 rounded border border-pir bg-pir-surface-0 p-4">
            <h2 className="text-label font-semibold text-pir-text-primary">{tt.ready.recapTitle}</h2>
            <div className="grid gap-2">
              <div className="rounded border border-pir bg-pir-base px-3 py-2 text-body text-pir-text-secondary">
                {fmt(tt.ready.folders, { count: sources.length })}
              </div>
              <div className="rounded border border-pir bg-pir-base px-3 py-2 text-body text-pir-text-secondary">
                {fmt(tt.ready.sources, { count: sourceCount(docsConsent, repoConsent) })}
              </div>
              <div className="rounded border border-pir bg-pir-base px-3 py-2 text-body text-pir-text-secondary">
                {fmt(tt.ready.cycle, { hour: cycleHour })}
              </div>
            </div>
            <div className="mt-2 rounded border border-pir-accent/40 bg-pir-accent/10 p-3">
              <h3 className="text-label font-semibold text-pir-text-primary">{tt.ready.initTitle}</h3>
              <code className="mt-2 block rounded border border-pir bg-pir-base px-3 py-2 font-mono text-caption text-pir-text-primary">
                marvis init
              </code>
              <p className="mt-2 text-body leading-6 text-pir-text-secondary">{tt.ready.initBody}</p>
            </div>
            <div className="rounded border border-pir-success/40 bg-pir-success/10 p-3">
              <h3 className="text-label font-semibold text-pir-text-primary">{tt.ready.demoTitle}</h3>
              <p className="mt-2 text-body leading-6 text-pir-text-secondary">{tt.ready.demoBody}</p>
            </div>
          </section>

          <section className="rounded border border-pir bg-pir-surface-0">
            <div className="border-b border-pir px-3 py-2">
              <h2 className="text-label font-semibold text-pir-text-primary">{tt.ready.setupPreview}</h2>
            </div>
            <pre className="max-h-[440px] min-h-[280px] overflow-auto whitespace-pre-wrap p-4 font-mono text-[11px] leading-5 text-pir-text-secondary">
              {setupPreview || tt.status.setupLoading}
            </pre>
          </section>
        </div>
      </>
    );
  }

  return (
    <WizardShell step={step} busy={busy} message={message} onSkipAll={() => onComplete()}>
      {body}
      <footer className="mt-auto flex flex-wrap items-center justify-between gap-3 border-t border-pir pt-5">
        <div className="flex gap-2">
          <button
            type="button"
            onClick={goBack}
            disabled={currentIndex === 0 || busy}
            className="h-9 rounded border border-pir px-3 text-label text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {tt.nav.back}
          </button>
          {!isLast && (
            <button
              type="button"
              onClick={skipStep}
              disabled={busy}
              className="h-9 rounded border border-pir px-3 text-label text-pir-text-tertiary hover:text-pir-text-primary disabled:opacity-50"
            >
              {tt.nav.skip}
            </button>
          )}
        </div>
        {isLast ? (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => onComplete()}
              disabled={busy}
              className="h-9 rounded border border-pir px-3 text-label text-pir-text-secondary hover:border-pir-accent hover:text-pir-text-primary disabled:opacity-50"
            >
              {tt.nav.finishNoTour}
            </button>
            <button
              type="button"
              onClick={startTour}
              disabled={busy}
              className="h-9 rounded bg-pir-accent px-4 text-label font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-60"
            >
              {busy ? tt.status.demoSeeding : tt.nav.startTour}
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={goNext}
            disabled={busy || (step === "folder" && !sourcesConfirmed)}
            className="h-9 rounded bg-pir-accent px-4 text-label font-semibold text-pir-base disabled:cursor-not-allowed disabled:opacity-60"
          >
            {tt.nav.next}
          </button>
        )}
      </footer>
    </WizardShell>
  );
}
