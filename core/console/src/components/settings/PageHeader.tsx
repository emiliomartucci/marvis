import type { ReactNode } from "react";

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  sub?: string;
  actions?: ReactNode;
  statusPill?: ReactNode;
}

export function PageHeader({
  eyebrow,
  title,
  sub,
  actions,
  statusPill,
}: PageHeaderProps) {
  return (
    <header className="flex items-start gap-4 border-b border-pir px-7 py-5">
      <div className="min-w-0 flex-1">
        {eyebrow && (
          <div className="mb-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] text-pir-text-muted">
            {eyebrow}
          </div>
        )}
        <div className="flex min-w-0 items-center gap-2.5">
          <h1 className="truncate font-display text-[24px] font-semibold leading-[1.2] tracking-normal text-pir-text-primary">
            {title}
          </h1>
          {statusPill}
        </div>
        {sub && (
          <p className="mt-1.5 max-w-[720px] text-body text-pir-text-secondary">
            {sub}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 items-center gap-2">
          {actions}
        </div>
      )}
    </header>
  );
}

export default PageHeader;
