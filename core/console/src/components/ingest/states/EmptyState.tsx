export function EmptyState() {
  return (
    <div className="flex h-full min-h-[360px] items-center justify-center bg-pir-base px-6 text-center">
      <div className="max-w-md">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-sm border border-pir bg-pir-surface-1 font-mono text-label font-bold text-pir-text-tertiary">
          00
        </div>
        <h2 className="font-display text-heading text-pir-text-primary">
          L&apos;apparato digerente e&apos; a riposo.
        </h2>
        <p className="mt-2 font-sans text-body text-pir-text-secondary">
          Carica file da Files, Folder o Zip. Se la coda resta vuota, verifica
          che il tuo utente abbia accesso ai project corretti.
        </p>
        <a
          href="/finder/"
          className="mt-5 inline-flex h-8 items-center rounded-sm border border-pir bg-pir-surface-1 px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
        >
          Vai a Finder
        </a>
        <div className="mt-6 grid grid-cols-3 gap-2 font-mono text-[10px] uppercase tracking-[0.08em] text-pir-text-muted">
          <span className="rounded-sm border border-pir bg-pir-surface-0 px-2 py-1">upload</span>
          <span className="rounded-sm border border-pir bg-pir-surface-0 px-2 py-1">parse</span>
          <span className="rounded-sm border border-pir bg-pir-surface-0 px-2 py-1">triage</span>
        </div>
      </div>
    </div>
  );
}
