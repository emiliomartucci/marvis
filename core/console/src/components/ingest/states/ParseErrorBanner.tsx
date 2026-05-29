interface ParseErrorBannerProps {
  message: string | null;
  busy?: boolean;
  onRetry: () => void;
  onQuarantine: () => void;
}

export function ParseErrorBanner({
  message,
  busy = false,
  onRetry,
  onQuarantine,
}: ParseErrorBannerProps) {
  return (
    <div className="border-b border-pir-error bg-pir-error/10 px-5 py-3" role="alert">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-error">
            Errore parser
          </p>
          <p className="mt-1 break-words font-mono text-caption leading-5 text-pir-text-secondary">
            {message || "Parser fallito senza dettaglio. Riprova o sposta in quarantine."}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onRetry}
            className="h-7 rounded-sm border border-pir-error bg-transparent px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-error transition-colors hover:bg-pir-error/10 disabled:cursor-wait disabled:opacity-50 focus:border-pir-accent focus:outline-none"
          >
            Riprova parser
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onQuarantine}
            className="h-7 rounded-sm border border-pir bg-pir-surface-1 px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary disabled:cursor-wait disabled:opacity-50 focus:border-pir-accent focus:outline-none"
          >
            Quarantine
          </button>
        </div>
      </div>
    </div>
  );
}
