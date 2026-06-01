"use client";

// Empty / loading / error panels for Brain tabs (sub-05 §4.13-4.15).
// Italian microcopy canonical per README §9.

export function PanelEmpty({ message }: { message: string }) {
  return (
    <div
      className="flex h-40 items-center justify-center border border-pir-border bg-[hsl(var(--pir-surface-1))] text-pir-text-secondary"
      style={{ borderRadius: "2px" }}
    >
      <span className="font-[var(--font-exo-2)] text-sm">{message}</span>
    </div>
  );
}

export function PanelLoading({ message }: { message: string }) {
  return (
    <div
      className="flex h-40 items-center justify-center border border-pir-border bg-[hsl(var(--pir-surface-1))]"
      style={{ borderRadius: "2px" }}
    >
      <span className="font-[var(--font-jetbrains-mono)] text-[12px] uppercase tracking-[0.18em] text-pir-text-tertiary">
        {message}
      </span>
    </div>
  );
}

/** @public */
export function PanelError({ message }: { message: string }) {
  return (
    <div
      className="flex h-40 items-center justify-center border border-[hsl(var(--pir-error)/0.4)] bg-[hsl(var(--pir-error)/0.06)] text-[hsl(var(--pir-error))]"
      style={{ borderRadius: "2px" }}
      role="alert"
    >
      <span className="font-[var(--font-exo-2)] text-sm">{message}</span>
    </div>
  );
}
