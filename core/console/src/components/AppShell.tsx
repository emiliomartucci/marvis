"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import { AuthProvider, useAuth } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { NotificationBell } from "@/components/notifications/NotificationBell";
import { ActionViewIcon } from "@/components/inbox/ActionViewIcon";
import { ActionViewModal } from "@/components/inbox/ActionViewModal";
import { useActionView } from "@/hooks/useActionView";
import { Logo } from "@/components/ui/Logo";
import { ThemeToggle } from "@/components/ui/ThemeToggle";
import { DesignV2Toggle } from "@/components/ui/DesignV2Toggle";
import { GlobalSearch } from "@/components/GlobalSearch";
import { PwaInstallButton } from "@/components/PwaInstallButton";
import ProjectNavigator from "@/components/projects/local/ProjectNavigator";
import { OnboardingWizard } from "@/components/onboarding/OnboardingWizard";
import { SpotlightTour } from "@/components/onboarding/SpotlightTour";
import { useDesignV2 } from "@/lib/useDesignV2";
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
import {
  readSessionsCountChangedDetail,
  SESSION_COUNT_CHANGED_EVENT,
} from "@/lib/sessionEvents";

interface AppShellProps {
  children: ReactNode;
  sidebar?: ReactNode;
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

interface PackageChild {
  href: string;
  label: string;
}

interface PackageNavItem {
  href: string;
  label: string;
  children?: PackageChild[];
}

const PACKAGES: PackageNavItem[] = [
  { href: "/terminal/", label: "Terminal" },
  { href: "/projects/", label: "Projects" },
  { href: "/triage/", label: "Triage" },
  {
    href: "/brain/",
    label: "Brain",
    children: [
      { href: "/brain/", label: "Oggi" },
      { href: "/brain/diario/", label: "Diario" },
      { href: "/brain/stride/", label: "Stride" },
      { href: "/brain/memoria/", label: "Memoria" },
      { href: "/brain/da-decidere/", label: "Da decidere" },
      { href: "/brain/cronologia/", label: "Cronologia" },
    ],
  },
  {
    href: "/inbox/",
    label: "Inbox",
    children: [
      { href: "/inbox/", label: "RSS" },
      { href: "/inbox/triage/files/", label: "Ingester" },
    ],
  },
  // SaaS-only nav: excluded from the OSS mirror; hosted/prod enables via env.
  ...(process.env.NEXT_PUBLIC_ENABLE_SAAS === "true"
    ? [
        { href: "/newsletter/", label: "Newsletter" },
        { href: "/automations/", label: "Automations" },
        { href: "/reddit/", label: "Reddit" },
      ]
    : []),
  { href: "/monitoring/", label: "Monitoring" },
  { href: "/finder/", label: "Finder" },
  ...(process.env.NEXT_PUBLIC_ENABLE_GRAPH_UX === "true"
    ? [{ href: "/graph/", label: "Graph" }]
    : []),
];

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

function isLocalMode(): boolean {
  const value = process.env.NEXT_PUBLIC_LOCAL_MODE;
  return value === "1" || value === "true";
}

/** Labels visibili solo a admin/super_admin (RBAC gate nav).
 *
 * Operator/viewer vedono solo Terminal/Projects/Triage/Inbox/Finder/Graph —
 * gli items qui sotto sono internal-only e vengono filtrati dal render in
 * TopbarV1 + TopbarV2. Introdotto 2026-05-13 per demo Gridfield. */
const ADMIN_ONLY_LABELS: ReadonlySet<string> = new Set([
  "Newsletter",
  "Automations",
  "Reddit",
  "Monitoring",
]);

function isAdminRole(role: string | null | undefined): boolean {
  return role === "super_admin" || role === "admin";
}

function filterPackagesByRole(
  packages: readonly PackageNavItem[],
  role: string | null | undefined,
): readonly PackageNavItem[] {
  if (isAdminRole(role)) return packages;
  return packages.filter((pkg) => !ADMIN_ONLY_LABELS.has(pkg.label));
}

function normalizeNavHref(href: string): string {
  return href.replace(/\/$/, "");
}

function isNavHrefActive(pathname: string, href: string): boolean {
  const normalized = normalizeNavHref(href);
  return pathname === normalized || pathname.startsWith(`${normalized}/`);
}

function isPackageActive(pathname: string, pkg: PackageNavItem): boolean {
  return isNavHrefActive(pathname, pkg.href) ||
    Boolean(pkg.children?.some((child) => isNavHrefActive(pathname, child.href)));
}

function getActiveChildHref(pathname: string, children: PackageChild[] = []): string | null {
  const active = children
    .filter((child) => isNavHrefActive(pathname, child.href))
    .sort((left, right) => normalizeNavHref(right.href).length - normalizeNavHref(left.href).length);
  return active[0]?.href ?? null;
}

function NavChevron({ className = "" }: { className?: string }) {
  return (
    <svg
      aria-hidden
      className={className}
      width="12"
      height="12"
      viewBox="0 0 20 20"
      fill="currentColor"
    >
      <path fillRule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clipRule="evenodd" />
    </svg>
  );
}

// --- v1 topbar (unchanged, default) ---

interface TopbarV1Props {
  pathname: string;
  sidebar?: ReactNode;
  mobileSidebarOpen: boolean;
  setMobileSidebarOpen: (v: boolean) => void;
  actionView: ReturnType<typeof useActionView>;
  permissions: { canAdmin: boolean };
  logout: () => void;
}

function TopbarV1({
  pathname,
  sidebar,
  mobileSidebarOpen,
  setMobileSidebarOpen,
  actionView,
  permissions,
  logout,
}: TopbarV1Props) {
  const { role } = useAuth();
  const visiblePackages = filterPackagesByRole(PACKAGES, role);
  return (
    <nav className="bg-pir-surface-0 border-b border-pir shrink-0">
      <div className="flex items-center px-4 h-10">
        {sidebar && (
          <button
            onClick={() => setMobileSidebarOpen(!mobileSidebarOpen)}
            className="md:hidden p-1 -ml-1 mr-2 text-pir-text-muted hover:text-pir-text-secondary touch-manipulation"
            aria-label="Toggle sidebar"
          >
            <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
              {mobileSidebarOpen ? (
                <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
              ) : (
                <path fillRule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 10a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
              )}
            </svg>
          </button>
        )}

        <Link href="/terminal/" className="mr-3 shrink-0 text-pir-text-primary hover:opacity-80 transition-opacity">
          <Logo size="sm" />
        </Link>

        <div className="flex items-center gap-1">
          {visiblePackages.map((pkg) => {
            const isActive = isPackageActive(pathname, pkg);
            const children = pkg.children ?? [];
            const activeChildHref = getActiveChildHref(pathname, children);
            return (
              <div key={pkg.href} className="group relative">
                <Link
                  href={pkg.href}
                  className={`inline-flex items-center gap-1 px-3 py-1.5 text-label transition-colors border-b-2 ${
                    isActive
                      ? "text-pir-accent border-b-pir-accent"
                      : "text-pir-text-tertiary hover:text-pir-text-secondary border-b-transparent"
                  }`}
                  aria-haspopup={children.length > 0 ? "menu" : undefined}
                >
                  {pkg.label}
                  {children.length > 0 && <NavChevron className="text-pir-text-muted" />}
                </Link>
                {children.length > 0 && (
                  <div
                    role="menu"
                    aria-label={`${pkg.label} navigation`}
                    className="invisible absolute left-0 top-full z-50 mt-1 min-w-36 border border-pir bg-pir-surface-0 py-1 opacity-0 transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
                  >
                    {children.map((child) => {
                      const childActive = activeChildHref === child.href;
                      return (
                        <Link
                          key={child.href}
                          href={child.href}
                          role="menuitem"
                          className={`block px-3 py-2 text-label transition-colors ${
                            childActive
                              ? "bg-pir-surface-1 text-pir-accent"
                              : "text-pir-text-tertiary hover:bg-pir-surface-1 hover:text-pir-text-secondary"
                          }`}
                        >
                          {child.label}
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div className="ml-4">
          <GlobalSearch />
        </div>

        <div className="ml-auto flex items-center gap-1">
          <DesignV2Toggle />
          <ThemeToggle />
          <ActionViewIcon
            unreadCount={actionView.unreadCount}
            onClick={actionView.openModal}
            isOpen={actionView.isOpen}
          />
          <NotificationBell />
          <PwaInstallButton />
          {permissions.canAdmin && (
            <Link
              href="/settings/users/"
              className={`p-1.5 rounded transition-colors ${
                pathname.startsWith("/settings")
                  ? "text-pir-accent"
                  : "text-pir-text-muted hover:text-pir-text-secondary"
              }`}
              aria-label="Settings"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
              </svg>
            </Link>
          )}
          <button
            onClick={logout}
            className="px-3 py-1 text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          >
            Logout
          </button>
        </div>
      </div>
    </nav>
  );
}

// --- v2 topbar (gated behind .theme-v2) ---
// Reuses TopbarV1Props as-is (same contract: pathname + sidebar slot + mobile
// toggle + actionView + permissions + logout). No need for a separate interface.

function TopbarV2(props: TopbarV1Props) {
  const { role } = useAuth();
  const visiblePackages = filterPackagesByRole(PACKAGES, role);
  return <TopbarV2Inner {...props} visiblePackages={visiblePackages} />;
}

interface TopbarV2InnerProps extends TopbarV1Props {
  visiblePackages: readonly PackageNavItem[];
}

function TopbarV2Inner({
  pathname,
  sidebar,
  mobileSidebarOpen,
  setMobileSidebarOpen,
  actionView,
  permissions,
  logout,
  visiblePackages,
}: TopbarV2InnerProps) {
  // Tab counters — fed from existing sources only. When no signal is available
  // the badge is omitted entirely (we never render a "0" counter).
  const [terminalSessionCount, setTerminalSessionCount] = useState<number | null>(null);
  const triageCount: number | null = null; // no hook today; future: triage unread
  const inboxUnread = actionView.unreadCount > 0 ? actionView.unreadCount : null;

  useEffect(() => {
    function handleSessionCountChanged(event: Event) {
      const count = readSessionsCountChangedDetail(event)?.count;
      if (typeof count === "number" && Number.isFinite(count) && count >= 0) {
        setTerminalSessionCount(count);
      }
    }

    window.addEventListener(SESSION_COUNT_CHANGED_EVENT, handleSessionCountChanged);
    return () => {
      window.removeEventListener(SESSION_COUNT_CHANGED_EVENT, handleSessionCountChanged);
    };
  }, []);

  return (
    <nav className="relative z-50 overflow-visible bg-pir-base border-b border-pir shrink-0 flex items-stretch h-16">
      {/* Logo wrap — 240px column aligned with sidebar */}
      <div className="hidden md:flex items-center w-[240px] min-w-[240px] px-4 bg-pir-surface-0 border-r border-pir text-pir-text-primary">
        <Link
          href="/terminal/"
          className="flex items-center text-pir-text-primary hover:opacity-80 transition-opacity"
          aria-label="Marvis home"
        >
          {/* V2 logo lockup — 56px tall per kit spec */}
          <span className="block" style={{ height: 56 }}>
            <Logo size="lg" />
          </span>
        </Link>
      </div>

      {/* Mobile hamburger — shown in place of logo on small screens */}
      {sidebar && (
        <button
          onClick={() => setMobileSidebarOpen(!mobileSidebarOpen)}
          className="md:hidden p-2 ml-2 self-center text-pir-text-tertiary hover:text-pir-text-primary"
          aria-label="Toggle sidebar"
        >
          <svg className="w-5 h-5" viewBox="0 0 20 20" fill="currentColor">
            {mobileSidebarOpen ? (
              <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
            ) : (
              <path fillRule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 10a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
            )}
          </svg>
        </button>
      )}

      {/* Tabs */}
      <div
        className="flex items-stretch flex-1 min-w-0 pl-1.5 overflow-x-auto md:overflow-visible"
        style={{ fontFamily: "var(--pir-font-sans)" }}
      >
        {visiblePackages.map((pkg) => {
          const isActive = isPackageActive(pathname, pkg);
          const children = pkg.children ?? [];
          const activeChildHref = getActiveChildHref(pathname, children);
          let count: number | null = null;
          if (pkg.label === "Terminal") count = terminalSessionCount;
          else if (pkg.label === "Triage") count = triageCount;
          else if (pkg.label === "Inbox") count = inboxUnread;
          return (
            <div key={pkg.href} className="group relative flex items-stretch">
              <Link
                href={pkg.href}
                className={`relative inline-flex items-center gap-1.5 px-3 whitespace-nowrap transition-colors ${
                  isActive
                    ? "text-pir-text-primary font-bold"
                    : "text-pir-text-tertiary hover:text-pir-text-primary font-medium"
                }`}
                style={{ fontSize: "13px", letterSpacing: "0.01em", lineHeight: 1 }}
                aria-haspopup={children.length > 0 ? "menu" : undefined}
              >
                {pkg.label}
                {children.length > 0 && <NavChevron className="text-pir-text-muted" />}
                {pkg.label === "Terminal" && isActive && (
                  <span className="font-mono text-[10px] font-medium text-pir-text-muted">
                    Sessioni
                  </span>
                )}
                {count != null && count > 0 && (
                  <span
                    className={`inline-flex items-center justify-center rounded-sm font-mono font-bold ${
                      pkg.label === "Inbox" ? "bg-pir-error text-white" : "bg-pir-warning text-[#1a1208]"
                    }`}
                    style={{
                      minWidth: 18,
                      height: 16,
                      padding: "0 5px",
                      fontSize: 10,
                      lineHeight: 1,
                      letterSpacing: "0.02em",
                    }}
                  >
                    {count > 99 ? "99+" : count}
                  </span>
                )}
                {isActive && (
                  <span
                    aria-hidden
                    className="absolute bg-pir-success"
                    style={{
                      left: 12,
                      right: 12,
                      bottom: 8,
                      height: 3,
                      borderRadius: 1,
                    }}
                  />
                )}
              </Link>
              {children.length > 0 && (
                <div
                  role="menu"
                  aria-label={`${pkg.label} navigation`}
                  className="invisible absolute left-0 top-full z-50 min-w-[170px] border border-pir bg-pir-surface-0 py-1 opacity-0 transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
                >
                  {children.map((child) => {
                    const childActive = activeChildHref === child.href;
                    return (
                      <Link
                        key={child.href}
                        href={child.href}
                        role="menuitem"
                        className={`block px-3 py-2 font-mono text-[11px] transition-colors ${
                          childActive
                            ? "bg-pir-surface-1 text-pir-accent"
                            : "text-pir-text-tertiary hover:bg-pir-surface-1 hover:text-pir-text-primary"
                        }`}
                      >
                        {child.label}
                      </Link>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Utils cluster — 30x30 IconBtns, search/theme/actionview/bell + admin + logout */}
      <div className="flex items-center gap-1 pr-2.5">
        <GlobalSearch />
        <DesignV2Toggle />
        <ThemeToggle />
        {/* ActionView v2-styled wrapper — keeps hook wiring, adjusts geometry */}
        <button
          onClick={actionView.openModal}
          className={`relative inline-flex items-center justify-center rounded-sm transition-colors ${
            actionView.isOpen
              ? "text-pir-text-primary bg-pir-surface-1"
              : "text-pir-text-tertiary hover:text-pir-text-primary hover:bg-pir-surface-1"
          }`}
          style={{ width: 30, height: 30 }}
          aria-label={`Action view${
            actionView.unreadCount > 0 ? ` (${actionView.unreadCount} unread)` : ""
          }`}
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <path d="M4 4h16v12H4z" />
            <path d="m4 4 8 6 8-6" />
          </svg>
          {actionView.unreadCount > 0 && (
            <span
              className="absolute bg-pir-accent font-mono font-bold text-center"
              style={{
                top: 4,
                right: 4,
                minWidth: 14,
                height: 14,
                padding: "0 3px",
                borderRadius: 7,
                color: "hsl(var(--pir-bone, 34 28% 88%))",
                fontSize: 9,
                lineHeight: "14px",
              }}
            >
              {actionView.unreadCount > 99 ? "99+" : actionView.unreadCount}
            </span>
          )}
        </button>
        <NotificationBell />
        <PwaInstallButton />
        {permissions.canAdmin && (
          <Link
            href="/settings/users/"
            className={`inline-flex items-center justify-center rounded-sm transition-colors ${
              pathname.startsWith("/settings")
                ? "text-pir-text-primary bg-pir-surface-1"
                : "text-pir-text-tertiary hover:text-pir-text-primary hover:bg-pir-surface-1"
            }`}
            style={{ width: 30, height: 30 }}
            aria-label="Settings"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="15"
              height="15"
              viewBox="0 0 20 20"
              fill="currentColor"
            >
              <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
            </svg>
          </Link>
        )}
        <button
          onClick={logout}
          className="px-2 text-pir-text-tertiary hover:text-pir-text-primary transition-colors font-mono"
          style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", height: 30 }}
        >
          Logout
        </button>
      </div>
    </nav>
  );
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
          href="/brain/diario/"
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

function AppShellContent({ children, sidebar }: AppShellProps) {
  const { status, logout, permissions } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const actionView = useActionView();
  const v2 = useDesignV2();

  useEffect(() => {
    if (status === "unauthenticated") {
      router.push("/login/");
    }
  }, [status, router]);

  useEffect(() => {
    setMobileSidebarOpen(false);
  }, [pathname]);

  if (status === "loading") {
    return (
      <div className="flex items-center justify-center h-screen bg-pir-base">
        <div className="text-pir-text-muted text-label">Loading...</div>
      </div>
    );
  }

  if (status === "unauthenticated") return null;

  return (
    <div data-testid="app-shell" className="flex flex-col h-screen overflow-hidden bg-pir-base">
      {v2 ? (
        <TopbarV2
          pathname={pathname}
          sidebar={sidebar}
          mobileSidebarOpen={mobileSidebarOpen}
          setMobileSidebarOpen={setMobileSidebarOpen}
          actionView={actionView}
          permissions={permissions}
          logout={logout}
        />
      ) : (
        <TopbarV1
          pathname={pathname}
          sidebar={sidebar}
          mobileSidebarOpen={mobileSidebarOpen}
          setMobileSidebarOpen={setMobileSidebarOpen}
          actionView={actionView}
          permissions={permissions}
          logout={logout}
        />
      )}

      {/* Body: sidebar + content */}
      <div className="flex-1 min-h-0 flex overflow-hidden">
        {/* Sidebar — desktop: fixed 240px, mobile: overlay */}
        {sidebar && (
          <>
            {/* Desktop sidebar */}
            <aside className="hidden md:flex w-[240px] shrink-0 flex-col bg-pir-surface-0 border-r border-pir overflow-y-auto">
              {sidebar}
            </aside>
            {/* Mobile sidebar overlay */}
            {mobileSidebarOpen && (
              <div className={`md:hidden fixed inset-0 ${v2 ? "top-16" : "top-10"} z-40 flex`}>
                <aside className="w-[280px] bg-pir-surface-0 border-r border-pir overflow-y-auto">
                  {sidebar}
                </aside>
                <div
                  className="flex-1 bg-black/40"
                  onClick={() => setMobileSidebarOpen(false)}
                />
              </div>
            )}
          </>
        )}

        {/* Content area */}
        <main className="flex-1 min-w-0 overflow-hidden flex flex-col">
          <LlmKeyMissingBanner />
          {children}
        </main>
      </div>

      {/* Action View modal */}
      {actionView.isOpen && (
        <ActionViewModal
          currentItem={actionView.currentItem}
          currentIndex={actionView.currentIndex}
          totalItems={actionView.totalItems}
          isExhausted={actionView.isExhausted}
          loading={actionView.loading}
          error={actionView.error}
          toastMessage={actionView.toastMessage}
          content={actionView.currentDetail?.content ?? null}
          tldr={actionView.tldr}
          tldrLoading={actionView.tldrLoading}
          deepResearch={actionView.deepResearch}
          deepResearchLoading={actionView.deepResearchLoading}
          sourceScores={actionView.sourceScores}
          onDecide={actionView.decide}
          onUndo={actionView.undo}
          onClose={actionView.closeModal}
          onClearError={actionView.clearError}
          onRequestTldr={actionView.requestTldr}
          onRequestDeepResearch={actionView.requestDeepResearch}
          onSaveInPlace={actionView.saveInPlace}
        />
      )}
    </div>
  );
}

export default function AppShell({ children, sidebar }: AppShellProps) {
  if (isLocalMode()) {
    // AuthProvider must wrap the local shell too: it runs getMe() (/auth/me),
    // which carries `capabilities.todos_llm_key_missing`. Without it the local
    // tier never fetched the capability, so `useAuth().llmKeyMissing` stayed
    // false and the honest-UX banner never rendered (gh #22 QA gap). On the
    // local single-user tier /auth/me returns 200 (loopback operator) — no
    // login redirect.
    return (
      <AuthProvider>
        <LocalAppShellContent sidebar={sidebar}>{children}</LocalAppShellContent>
      </AuthProvider>
    );
  }

  return (
    <AuthProvider>
      <AppShellContent sidebar={sidebar}>{children}</AppShellContent>
    </AuthProvider>
  );
}
