// v1.0.0 - 2026-04-22 - Shared shell for /projects single-pager v2 sections (PR #9)
"use client";

import type { ReactNode } from "react";

interface SectionShellProps {
  eyebrow: string;
  title: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  anchorId?: string;
}

function SectionShell({ eyebrow, title, action, children, anchorId }: SectionShellProps) {
  return (
    <section
      id={anchorId}
      className="border-b border-pir last:border-b-0"
      style={{ padding: "28px 32px 24px 32px" }}
    >
      <header className="flex items-center justify-between mb-3.5">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-pir-text-tertiary leading-none">
            {eyebrow}
          </div>
          <div
            className="text-pir-text-primary leading-tight mt-[3px]"
            style={{
              fontFamily: "var(--pir-font-sans, system-ui)",
              fontSize: 16,
              fontWeight: 600,
              letterSpacing: "-0.01em",
            }}
          >
            {title}
          </div>
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </header>
      {children}
    </section>
  );
}

export default SectionShell;
