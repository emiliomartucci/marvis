"use client";

import type { SessionCatalogProvider, SessionProvider } from "@/lib/types";

interface ProviderSelectorProps {
  value: SessionProvider;
  onChange: (provider: SessionProvider) => void;
  providers: SessionCatalogProvider[];
  disabled?: boolean;
}

export default function ProviderSelector({
  value,
  onChange,
  providers,
  disabled,
}: ProviderSelectorProps) {
  return (
    <div className="grid grid-cols-2 gap-2">
      {providers.map((p) => (
        <button
          key={p.id}
          type="button"
          disabled={disabled}
          onClick={() => onChange(p.id)}
          className={`rounded-lg border px-3 py-2 text-left transition-colors ${
            value === p.id
              ? "border-pir-accent bg-[hsl(var(--pir-accent)/0.14)] text-pir-text-primary"
              : "border-pir bg-pir-surface-1 text-pir-text-secondary hover:text-pir-text-primary"
          } disabled:opacity-50 disabled:cursor-not-allowed`}
        >
          <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-pir-text-tertiary">
            {p.id}
          </div>
          <div className="mt-1 text-sm font-medium">{p.label}</div>
          <div className="mt-1 text-[10px] font-mono text-pir-text-muted">
            {p.default_model}
          </div>
        </button>
      ))}
    </div>
  );
}
