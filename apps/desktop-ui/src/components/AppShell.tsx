"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import { AuthProvider, useAuth } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ThemeToggle } from "@/components/ui/ThemeToggle";
import ProjectNavigator from "@/components/projects/local/ProjectNavigator";
import { OnboardingWizard } from "@/components/onboarding/OnboardingWizard";
import { SpotlightTour } from "@/components/onboarding/SpotlightTour";
import { API_BASE_URL, APP_BASE_PATH } from "@/lib/config";
import { useT } from "@/lib/i18n";
import {
  markDemoRemoved,
  markDemoSeeded,
  markOnboardingDone,
  shouldShowOnboarding,
} from "@/lib/onboarding";
import { createTodoLocal, listTodosLocal } from "@/lib/api";
import { TODOS_CHANGED_EVENT, notifyTodosChanged } from "@/lib/todosEvents";
import { usePollingData } from "@/hooks/usePollingData";
import { openTodos } from "@/components/todos/todoModel";
import type { TourPart } from "@/lib/tour";

interface AppShellProps {
  children: ReactNode;
}

// Honest-UX banner dismissal (gh #22 round 2): the dismiss is NOT permanent —
// it expires after 24h or when the calendar day rolls over, whichever comes
// first. We store the dismissal timestamp in localStorage and re-show after the
// TTL so a degraded classifier never stays hidden forever.
const LLM_BANNER_DISMISS_KEY = "marvis:llm-key-banner-dismissed-at";
const LLM_BANNER_DISMISS_TTL_MS = 24 * 60 * 60 * 1000;

function readBannerDismissed(now: number): boolean {
  if (typeof window === "undefined") return false;
  try {
    const raw = window.localStorage.getItem(LLM_BANNER_DISMISS_KEY);
    if (!raw) return false;
    const dismissedAt = Number(raw);
    if (!Number.isFinite(dismissedAt)) return false;
    // Expired by TTL → re-show.
    if (now - dismissedAt >= LLM_BANNER_DISMISS_TTL_MS) return false;
    // New calendar day → re-show even if within 24h.
    if (new Date(dismissedAt).toDateString() !== new Date(now).toDateString()) return false;
    return true;
  } catch {
    return false;
  }
}

/**
 * Honest-UX banner (gh #22): when the backend reports the gateway LLM key is
 * missing, todos auto-classification has silently degraded to the heuristic.
 * Surface it instead of failing silently, with a link to configure a key.
 * Dismissable but the dismissal expires (see readBannerDismissed). Renders
 * nothing when the key is present (managed server) — so it self-gates.
 */
function LlmKeyMissingBanner() {
  const { llmKeyMissing } = useAuth();
  const { t } = useT();
  // SSR-safe: start not-dismissed (matches the prerendered HTML) and reconcile
  // against localStorage only after mount to avoid a hydration mismatch.
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    setDismissed(readBannerDismissed(Date.now()));
  }, []);

  if (!llmKeyMissing || dismissed) return null;

  function handleDismiss() {
    setDismissed(true);
    try {
      window.localStorage.setItem(LLM_BANNER_DISMISS_KEY, String(Date.now()));
    } catch {
      // Read-only storage should not break the dismiss (stays hidden this load).
    }
  }

  return (
    <div
      role="status"
      data-testid="llm-key-missing-banner"
      className="shrink-0 border-b border-pir-warning/40 bg-pir-warning/10 px-4 py-2 text-pir-text-secondary"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-label font-semibold text-pir-text-primary">{t.appShell.llmKeyMissingTitle}</p>
          <p className="text-caption text-pir-text-tertiary">
            {t.appShell.llmKeyMissingHint}{" "}
            <Link href="/settings/llm/" className="text-pir-accent underline underline-offset-2 hover:brightness-110">
              {t.appShell.llmKeyMissingLink}
            </Link>
          </p>
        </div>
        <button
          type="button"
          onClick={handleDismiss}
          aria-label={t.appShell.llmKeyMissingDismissLabel}
          className="shrink-0 rounded border border-pir px-2 py-0.5 text-caption text-pir-text-muted transition-colors hover:text-pir-text-primary"
        >
          {t.appShell.llmKeyMissingDismiss}
        </button>
      </div>
    </div>
  );
}

type LocalNavKey = "diario" | "todos" | "task" | "projects" | "universe";

interface LocalNavItem {
  key: LocalNavKey;
  href: string;
}

const LOCAL_NAV: LocalNavItem[] = [
  { key: "diario", href: "/diario/" },
  { key: "todos", href: "/todos/" },
  { key: "task", href: "/tasks/" },
  { key: "projects", href: "/projects/" },
  { key: "universe", href: "/universe/" },
];

interface MonitoringVersion {
  installed: string;
  latest: string | null;
  update_available: boolean;
}

function normalizeNavHref(href: string): string {
  return href.replace(/\/$/, "");
}

function isNavHrefActive(pathname: string, href: string): boolean {
  const normalized = normalizeNavHref(href);
  return pathname === normalized || pathname.startsWith(`${normalized}/`);
}

function localRouteKey(pathname: string): LocalNavKey {
  if (isNavHrefActive(pathname, "/todos/")) return "todos";
  if (isNavHrefActive(pathname, "/tasks/")) return "task";
  if (isNavHrefActive(pathname, "/projects/")) return "projects";
  if (isNavHrefActive(pathname, "/universe/")) return "universe";
  return "diario";
}

function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
  return Promise.resolve();
}

async function fetchMonitoringVersion(signal: AbortSignal): Promise<MonitoringVersion> {
  const response = await fetch(`${API_BASE_URL}/api/v1/monitoring/version`, {
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}

function LocalSidebar({
  pathname,
  onToast,
  onOpenWizard,
  onStartTour,
}: {
  pathname: string;
  onToast: (message: string) => void;
  onOpenWizard: () => void;
  onStartTour: (part: TourPart) => void;
}) {
  const { t, locale, setLocale } = useT();
  const router = useRouter();
  const [capture, setCapture] = useState("");
  const [submittingCapture, setSubmittingCapture] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const activeKey = localRouteKey(pathname);
  const fetchOpenTodos = useCallback(
    (signal: AbortSignal) => listTodosLocal({ status: "aperto,in_revisione", limit: 500 }, { signal }),
    []
  );
  const { data: badgeTodos, refresh: refreshTodosBadge } = usePollingData(fetchOpenTodos, {
    interval: 20_000,
    backoff: true,
    unchangedThreshold: 4,
  });
  const todosBadge = badgeTodos ? openTodos(badgeTodos).length : null;

  useEffect(() => {
    function handleTodosChanged() {
      refreshTodosBadge();
    }
    window.addEventListener(TODOS_CHANGED_EVENT, handleTodosChanged);
    return () => window.removeEventListener(TODOS_CHANGED_EVENT, handleTodosChanged);
  }, [refreshTodosBadge]);

  async function handleQuickCaptureSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = capture.trim();
    if (!text || submittingCapture) return;
    setSubmittingCapture(true);
    try {
      await createTodoLocal({ text });
      onToast(t.todos.captions.captured);
      setCapture("");
      notifyTodosChanged();
      refreshTodosBadge();
      router.push("/todos/");
    } catch {
      onToast(t.todos.captions.actionFailed);
    } finally {
      setSubmittingCapture(false);
    }
  }

  const labels: Record<LocalNavKey, string> = {
    diario: t.appShell.nav.diario,
    todos: t.appShell.nav.todos,
    task: t.appShell.nav.task,
    projects: t.appShell.nav.projects,
    universe: t.appShell.nav.universe,
  };

  return (
    <aside
      data-testid="local-sidebar"
      className="flex h-full w-[244px] shrink-0 flex-col border-r border-pir bg-pir-surface-0 text-pir-text-primary"
    >
      <div className="px-4 pb-3 pt-4">
        {/* Brand lockup per the Claude Design mock (MarvisLogo in app/icons.js):
            MX-01 bot mark + "marvis" display over a letter-spaced mono
            "CONSOLE". logo-lockup.svg is the OLD "marvisx · CONSOLE PIR"
            brand and embeds its own bot, which doubled the mark in the beta. */}
        <Link
          href="/diario/"
          className="flex items-center gap-2.5 text-pir-text-primary transition-opacity hover:opacity-80"
          aria-label={t.appShell.logoAlt}
        >
          {/* eslint-disable-next-line @next/next/no-img-element -- Local shell brief requires reusing public SVG assets directly. */}
          <img
            src={`${APP_BASE_PATH}/bot-mark.svg`}
            alt=""
            className="h-[34px] w-auto shrink-0"
          />
          <span className="flex min-w-0 flex-col gap-px leading-none">
            <span className="font-sans text-[21px] font-bold leading-none tracking-[-0.01em]">
              marvis
            </span>
            <span className="font-mono text-[9px] font-semibold uppercase leading-none tracking-[0.22em] text-pir-text-muted">
              CONSOLE
            </span>
          </span>
        </Link>
      </div>

      <form className="px-3 pb-3" onSubmit={handleQuickCaptureSubmit}>
        <input
          data-tour="capture"
          value={capture}
          onChange={(event) => setCapture(event.target.value)}
          disabled={submittingCapture}
          aria-label={t.appShell.quickCaptureLabel}
          placeholder={t.appShell.quickCapturePlaceholder}
          className="h-[34px] w-full rounded border border-pir bg-pir-surface-1 px-3 text-body text-pir-text-primary outline-none transition-colors placeholder:text-pir-text-muted focus:border-pir-accent disabled:cursor-not-allowed disabled:opacity-60"
        />
      </form>

      <nav className="flex flex-col gap-0.5 px-2 pb-3" aria-label={t.appShell.navLabel}>
        {LOCAL_NAV.map((item) => {
          const active = activeKey === item.key;
          const isTodos = item.key === "todos";
          return (
            <Link
              key={item.key}
              href={item.href}
              data-tour={item.key}
              className={`flex h-9 items-center justify-between rounded px-3 text-label transition-colors ${
                active
                  ? "border border-pir-accent bg-pir-accent/10 text-pir-text-primary"
                  : "border border-transparent text-pir-text-tertiary hover:bg-pir-surface-1 hover:text-pir-text-primary"
              }`}
            >
              <span>{labels[item.key]}</span>
              {isTodos && todosBadge != null && todosBadge > 0 && (
                <span
                  className="min-w-5 rounded bg-pir-accent px-1.5 py-0.5 text-center font-mono text-[10px] font-bold text-pir-base"
                  aria-label={t.appShell.todosBadgeLabel}
                >
                  {todosBadge > 99 ? "99+" : todosBadge}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      <div className="mx-4 border-t border-pir" />

      <div className="min-h-0 flex-1 px-4 py-3">
        <Suspense fallback={null}>
          <ProjectNavigator />
        </Suspense>
      </div>

      <div className="border-t border-pir px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1">
            <span data-tour="theme" className="inline-flex">
              <ThemeToggle />
            </span>
            <div className="relative">
              <button
                type="button"
                onClick={() => setHelpOpen((open) => !open)}
                aria-label={t.appShell.helpLabel}
                className="h-7 w-7 rounded text-pir-text-muted transition-colors hover:bg-pir-surface-1 hover:text-pir-text-primary"
              >
                ?
              </button>
              {helpOpen && (
                <div className="absolute bottom-9 left-0 z-50 w-40 rounded border border-pir bg-pir-surface-0 p-1 shadow-xl">
                  <button
                    type="button"
                    onClick={() => {
                      setHelpOpen(false);
                      onOpenWizard();
                    }}
                    className="block w-full rounded px-2 py-1.5 text-left text-caption text-pir-text-secondary hover:bg-pir-surface-1 hover:text-pir-text-primary"
                  >
                    {t.appShell.helpWizard}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setHelpOpen(false);
                      onStartTour(2);
                    }}
                    className="block w-full rounded px-2 py-1.5 text-left text-caption text-pir-text-secondary hover:bg-pir-surface-1 hover:text-pir-text-primary"
                  >
                    {t.appShell.helpTour}
                  </button>
                </div>
              )}
            </div>
          </div>
          <div data-tour="locale" className="flex items-center gap-1" aria-label={t.appShell.localeLabel}>
            {(["it", "en"] as const).map((nextLocale) => (
              <button
                key={nextLocale}
                type="button"
                onClick={() => setLocale(nextLocale)}
                className={`h-6 rounded px-1.5 font-mono text-[10px] uppercase transition-colors ${
                  locale === nextLocale
                    ? "bg-pir-accent text-pir-base"
                    : "text-pir-text-muted hover:bg-pir-surface-1 hover:text-pir-text-primary"
                }`}
              >
                {nextLocale}
              </button>
            ))}
          </div>
          <span className="flex min-w-0 items-center gap-2 text-caption text-pir-text-muted">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-pir-success" aria-hidden />
            <span className="font-mono">{t.appShell.localStatus}</span>
          </span>
        </div>
      </div>
    </aside>
  );
}

function LocalStatusBar() {
  const { t } = useT();
  const [version, setVersion] = useState<MonitoringVersion | null>(null);
  const [copied, setCopied] = useState(false);
  const updateCommand = "pip install -U marvisx-cli";
  const updateAvailable = version?.update_available === true;
  const installed = version?.installed
    ? `v${version.installed.replace(/^v/, "")}`
    : t.appShell.versionUnavailable;

  useEffect(() => {
    const controller = new AbortController();
    fetchMonitoringVersion(controller.signal)
      .then(setVersion)
      .catch(() => setVersion(null));
    return () => controller.abort();
  }, []);

  async function copyUpdateCommand() {
    if (!updateAvailable) return;
    await writeClipboard(updateCommand);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

  return (
    <footer
      data-testid="local-status-bar"
      className="flex h-7 shrink-0 items-center justify-between border-t border-pir bg-pir-surface-0 px-3 text-caption text-pir-text-muted"
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-pir-success" aria-hidden />
        <span>{t.appShell.localStatus}</span>
        <span aria-hidden>·</span>
        <span className="font-mono">localhost:8100</span>
      </div>

      <div className="flex min-w-0 items-center gap-3">
        <span className="hidden font-mono text-caption sm:inline">{t.appShell.hostedSoon}</span>
        <button
          type="button"
          onClick={copyUpdateCommand}
          disabled={!updateAvailable}
          aria-label={updateAvailable ? t.appShell.updateCommandLabel : undefined}
          className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 font-mono text-caption transition-colors ${
            updateAvailable
              ? "text-pir-warning hover:bg-pir-surface-1 hover:text-pir-text-primary"
              : "text-pir-text-muted"
          }`}
        >
          {updateAvailable && (
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-pir-warning" aria-hidden />
          )}
          <span>{installed}</span>
          {updateAvailable && version?.latest && (
            <span className="hidden sm:inline">
              {t.appShell.versionLatestPrefix} v{version.latest.replace(/^v/, "")}
            </span>
          )}
        </button>
        {updateAvailable && (
          <code className="hidden max-w-[220px] truncate font-mono text-[10px] text-pir-text-muted md:inline">
            {copied ? t.appShell.updateCommandCopied : updateCommand}
          </code>
        )}
      </div>
    </footer>
  );
}

function LocalToast({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-10 left-[260px] z-50 rounded border border-pir bg-pir-surface-1 px-3 py-2 text-label text-pir-text-primary"
    >
      {message}
    </div>
  );
}

function LocalAppShellContent({ children }: AppShellProps) {
  const pathname = usePathname();
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [tourPart, setTourPart] = useState<TourPart | null>(null);
  const route = localRouteKey(pathname);

  useEffect(() => {
    try {
      setWizardOpen(shouldShowOnboarding(window.localStorage));
    } catch {
      setWizardOpen(false);
    }
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem("marvis:route", route);
    } catch {
      // Static export should keep rendering if storage is unavailable.
    }
  }, [route]);

  useEffect(() => {
    if (!toastMessage) return;
    const timeout = window.setTimeout(() => setToastMessage(null), 2200);
    return () => window.clearTimeout(timeout);
  }, [toastMessage]);

  return (
    <div
      data-testid="app-shell"
      data-local-mode="true"
      className="flex h-screen flex-col overflow-hidden bg-pir-base text-[14px] text-pir-text-primary"
    >
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <LocalSidebar
          pathname={pathname}
          onToast={setToastMessage}
          onOpenWizard={() => setWizardOpen(true)}
          onStartTour={setTourPart}
        />
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-pir-base">
          <LlmKeyMissingBanner />
          {children}
        </main>
      </div>
      <LocalStatusBar />
      <LocalToast message={toastMessage} />
      {wizardOpen && (
        <OnboardingWizard
          onComplete={(options) => {
            try {
              markOnboardingDone(window.localStorage);
              if (options?.demoSeeded) markDemoSeeded(window.localStorage);
            } catch {
              // Storage failure should not trap the user in the wizard.
            }
            setWizardOpen(false);
            if (options?.startTour) setTourPart(1);
          }}
        />
      )}
      {tourPart && (
        <SpotlightTour
          part={tourPart}
          onClose={() => setTourPart(null)}
          onDemoRemoved={() => {
            try {
              markDemoRemoved(window.localStorage);
            } catch {
              // Non-fatal: backend teardown already ran.
            }
          }}
        />
      )}
    </div>
  );
}

// The shared shell branched on NEXT_PUBLIC_LOCAL_MODE between this shell and a
// hosted one whose top bar navigated to Terminal, Triage, Brain, Inbox,
// Monitoring and Finder. The flag hid it at runtime; the import shipped it in
// the bundle regardless, so the local artifact carried the hosted product's
// navigation — the same shape as the 2026-07-23 terminal incident. This product
// has one shell.
export default function AppShell({ children }: AppShellProps) {
  // AuthProvider wraps the local shell too: it runs getMe() (/auth/me), which
  // carries `capabilities.todos_llm_key_missing`. Without it the local tier
  // never fetched the capability, so `useAuth().llmKeyMissing` stayed false and
  // the honest-UX banner never rendered (gh #22 QA gap). On the local
  // single-user tier /auth/me returns 200 (loopback operator) — no login
  // redirect.
  return (
    <AuthProvider>
      <LocalAppShellContent>{children}</LocalAppShellContent>
    </AuthProvider>
  );
}
