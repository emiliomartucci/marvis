// Shared shell so every step has identical chrome (title + sub + controls).

"use client";

import type { ReactNode } from "react";

interface WizardCardProps {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  controls: ReactNode;
}

export default function WizardCard({
  title,
  description,
  children,
  controls,
}: WizardCardProps) {
  return (
    <div className="rounded border border-pir bg-pir-surface-0 p-6">
      <h2 className="mb-2 text-xl font-semibold text-pir-text-primary">
        {title}
      </h2>
      {description ? (
        <div className="mb-6 text-sm text-pir-text-secondary">
          {description}
        </div>
      ) : null}

      <div className="space-y-4">{children}</div>

      <div className="mt-8 flex items-center justify-between border-t border-pir pt-4">
        {controls}
      </div>
    </div>
  );
}
