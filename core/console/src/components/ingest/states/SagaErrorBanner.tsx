interface SagaErrorBannerProps {
  message: string | null;
  step?: string | null;
}

export function SagaErrorBanner({ message, step }: SagaErrorBannerProps) {
  return (
    <div className="border-b border-pir-warning bg-pir-warning/10 px-5 py-3" role="alert">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-warning">
            Errore saga
          </p>
          <p className="mt-1 break-words font-mono text-caption leading-5 text-pir-text-secondary">
            Insert failed at step {step || "unknown"}. Manual review required.
          </p>
          {message && (
            <p className="mt-1 break-words font-mono text-caption leading-5 text-pir-text-muted">
              {message}
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            className="h-7 rounded-sm border border-pir-warning bg-transparent px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-warning transition-colors hover:bg-pir-warning/10 focus:border-pir-accent focus:outline-none"
          >
            Apri log
          </button>
          <button
            type="button"
            className="h-7 rounded-sm border border-pir bg-pir-surface-1 px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
          >
            Manual done
          </button>
        </div>
      </div>
    </div>
  );
}
