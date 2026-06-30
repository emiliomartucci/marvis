"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useT } from "@/lib/i18n";

type SettingsNavItem = {
  href: string;
  label: string;
  icon: ReactNode;
  soon?: boolean;
};

type SettingsNavGroup = {
  label: string;
  items: SettingsNavItem[];
};

function isLocalMode(): boolean {
  // Build-time inlined (NEXT_PUBLIC_*), so SSR and client agree — no hydration
  // drift. Mirrors AppShell.isLocalMode().
  const value = process.env.NEXT_PUBLIC_LOCAL_MODE;
  return value === "1" || value === "true";
}

function buildSettingsGroups(llmLabel: string, localMode: boolean): SettingsNavGroup[] {
  const accessItems: SettingsNavItem[] = [
    {
      href: "/settings/users",
      label: "Users",
      icon: <UsersIcon />,
    },
  ];
  // Teams + Roles & Permissions are multi-user / managed concepts — hidden on the
  // local single-user (OSS) tier where there is one operator and one org (gh #33),
  // mirroring AppShell's local-mode nav gating.
  if (!localMode) {
    accessItems.push(
      {
        href: "/settings/teams",
        label: "Teams",
        icon: <TeamsIcon />,
      },
      {
        href: "/settings/workspace",
        label: "Roles & Permissions",
        icon: <ShieldIcon />,
      },
    );
  }

  return [
    {
      label: "Access",
      items: accessItems,
    },
    {
      label: "Integrations",
      items: [
        {
          href: "/settings/tokens",
          label: "Tokens",
          icon: <KeyIcon />,
        },
        {
          href: "/settings/ingest-keys",
          label: "Ingest Keys",
          icon: <UploadIcon />,
          soon: true,
        },
        {
          href: "/settings/llm",
          label: llmLabel,
          icon: <CpuIcon />,
        },
      ],
    },
    {
      label: "System",
      items: [
        {
          href: "/settings/activity",
          label: "Activity",
          icon: <ActivityIcon />,
        },
        {
          href: "/settings/general",
          label: "System / About",
          icon: <InfoIcon />,
        },
      ],
    },
  ];
}

function normalizePath(path: string): string {
  return path.replace(/\/$/, "");
}

function isItemActive(pathname: string, href: string): boolean {
  const normalized = normalizePath(href);
  return pathname === normalized || pathname.startsWith(`${normalized}/`);
}

export default function SettingsSidebar() {
  const pathname = usePathname();
  const { t } = useT();
  const settingsGroups = buildSettingsGroups(t.llmSettings.title, isLocalMode());

  return (
    <div className="flex min-h-full flex-col bg-pir-surface-0">
      <div className="border-b border-pir px-3.5 py-3">
        <div className="font-mono text-[11px] font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">
          Settings
        </div>
        <div className="mt-0.5 font-mono text-[10px] tracking-[0.02em] text-pir-text-muted">
          oss · single-org
        </div>
      </div>

      <nav className="flex flex-col gap-2 px-2 py-3" aria-label="Settings">
        {settingsGroups.map((group) => (
          <section key={group.label} aria-labelledby={`settings-group-${group.label.toLowerCase()}`}>
            <div
              id={`settings-group-${group.label.toLowerCase()}`}
              className="px-2 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-pir-text-muted"
            >
              {group.label}
            </div>
            <div className="flex flex-col gap-px">
              {group.items.map((item) => (
                <SettingsNavItem key={item.href} item={item} isActive={isItemActive(pathname, item.href)} />
              ))}
            </div>
          </section>
        ))}
      </nav>
    </div>
  );
}

function SettingsNavItem({ item, isActive }: { item: SettingsNavItem; isActive: boolean }) {
  const className = [
    "flex min-h-8 items-center gap-2.5 border-l-2 px-2 py-1.5 text-label transition-colors",
    isActive
      ? "border-l-pir-accent bg-[hsl(var(--pir-accent)/0.14)] text-pir-accent"
      : "border-l-transparent text-pir-text-secondary hover:bg-pir-surface-1 hover:text-pir-text-primary",
    item.soon ? "cursor-default opacity-55 hover:bg-transparent hover:text-pir-text-secondary" : "",
  ].join(" ");

  const content = (
    <>
      <span className={`shrink-0 ${isActive ? "text-pir-accent" : "text-pir-text-muted"}`}>
        {item.icon}
      </span>
      <span className="min-w-0 flex-1 truncate">{item.label}</span>
      {item.soon && (
        <span className="ml-auto shrink-0 rounded-sm border border-pir px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-[0.1em] text-pir-text-muted">
          soon
        </span>
      )}
    </>
  );

  if (item.soon) {
    return (
      <div className={className} aria-disabled="true">
        {content}
      </div>
    );
  }

  return (
    <Link href={`${item.href}/`} className={className}>
      {content}
    </Link>
  );
}

function UsersIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path d="M9 6a3 3 0 11-6 0 3 3 0 016 0zM17 6a3 3 0 11-6 0 3 3 0 016 0zM12.93 17c.046-.327.07-.66.07-1a6.97 6.97 0 00-1.5-4.33A5 5 0 0119 16v1h-6.07zM6 11a5 5 0 015 5v1H1v-1a5 5 0 015-5z" />
    </svg>
  );
}

function TeamsIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path d="M13 7a3 3 0 11-6 0 3 3 0 016 0zM4 15a5 5 0 1110 0v1H4v-1z" />
      <path d="M16.25 8.5a2.25 2.25 0 11-2.75-2.19 4.5 4.5 0 012.75 2.19zM17.5 16h-2.1a6.47 6.47 0 00-.84-3.1 3.75 3.75 0 012.94 3.1z" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M10 1.944l6 2.25v4.311c0 3.768-2.296 7.158-5.798 8.555L10 17.14l-.202-.08C6.296 15.663 4 12.273 4 8.505V4.194l6-2.25zm2.707 6.763a1 1 0 00-1.414-1.414L9 9.586l-.793-.793a1 1 0 00-1.414 1.414l1.5 1.5a1 1 0 001.414 0l3-3z" clipRule="evenodd" />
    </svg>
  );
}

function KeyIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M18 8a6 6 0 01-7.743 5.743L10 14l-1 1-1 1H6v2H2v-4l4.257-4.257A6 6 0 1118 8zm-6-4a1 1 0 100 2 2 2 0 012 2 1 1 0 102 0 4 4 0 00-4-4z" clipRule="evenodd" />
    </svg>
  );
}

function UploadIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M3 14a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm7-11a1 1 0 01.707.293l4 4a1 1 0 01-1.414 1.414L11 6.414V11a1 1 0 11-2 0V6.414L6.707 8.707a1 1 0 01-1.414-1.414l4-4A1 1 0 0110 3z" clipRule="evenodd" />
    </svg>
  );
}

function CpuIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path d="M6 6h8v8H6V6z" />
      <path fillRule="evenodd" d="M4 2a1 1 0 011 1v1h1V3a1 1 0 112 0v1h1V3a1 1 0 112 0v1h1V3a1 1 0 112 0v1h1a1 1 0 011 1v1h1a1 1 0 110 2h-1v1h1a1 1 0 110 2h-1v1h1a1 1 0 110 2h-1v1a1 1 0 01-1 1h-1v1a1 1 0 11-2 0v-1h-1v1a1 1 0 11-2 0v-1H8v1a1 1 0 11-2 0v-1H5a1 1 0 01-1-1v-1H3a1 1 0 110-2h1v-1H3a1 1 0 110-2h1V8H3a1 1 0 010-2h1V5a1 1 0 011-1h1V3a1 1 0 011-1zm2 4v8h8V6H6z" clipRule="evenodd" />
    </svg>
  );
}

function ActivityIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clipRule="evenodd" />
    </svg>
  );
}

function InfoIcon() {
  return (
    <svg aria-hidden className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M18 10A8 8 0 112 10a8 8 0 0116 0zM9 8a1 1 0 112 0v6a1 1 0 11-2 0V8zm1-4a1.25 1.25 0 100 2.5A1.25 1.25 0 0010 4z" clipRule="evenodd" />
    </svg>
  );
}
