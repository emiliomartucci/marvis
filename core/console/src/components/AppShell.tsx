"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import { AuthProvider, useAuth } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { NotificationBell } from "@/components/notifications/NotificationBell";
import { ActionViewIcon } from "@/components/inbox/ActionViewIcon";
import { ActionViewModal } from "@/components/inbox/ActionViewModal";
import { useActionView } from "@/hooks/useActionView";
import { Logo } from "@/components/ui/Logo";
import { ThemeToggle } from "@/components/ui/ThemeToggle";
import { DesignV2Toggle } from "@/components/ui/DesignV2Toggle";
import { GlobalSearch } from "@/components/GlobalSearch";
import { useDesignV2 } from "@/lib/useDesignV2";

interface AppShellProps {
  children: ReactNode;
  sidebar?: ReactNode;
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
  const triageCount: number | null = null; // no hook today; future: triage unread
  const inboxUnread = actionView.unreadCount > 0 ? actionView.unreadCount : null;

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
          if (pkg.label === "Triage") count = triageCount;
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
  return (
    <AuthProvider>
      <AppShellContent sidebar={sidebar}>{children}</AppShellContent>
    </AuthProvider>
  );
}
