interface ConnectionErrorToastProps {
  visible?: boolean;
}

export function ConnectionErrorToast({ visible = true }: ConnectionErrorToastProps) {
  if (!visible) return null;
  return (
    <div className="pointer-events-none fixed left-1/2 top-3 z-50 -translate-x-1/2">
      <div
        role="status"
        className="pointer-events-auto rounded-sm border border-pir-warning bg-pir-surface-0 px-3 py-2 font-mono text-caption text-pir-warning shadow-none"
      >
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 animate-pulse rounded-full bg-pir-warning" aria-hidden="true" />
          <span>WS disconnesso, riconnessione in corso...</span>
        </div>
        <p className="mt-1 font-sans text-[10px] leading-4 text-pir-text-tertiary">
          La lista resta visibile; gli eventi realtime riprendono appena il socket torna.
        </p>
        <div className="mt-2 h-px bg-pir-warning/20" aria-hidden="true" />
        <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.08em] text-pir-text-muted">
          fallback: refresh manuale
        </p>
      </div>
    </div>
  );
}
