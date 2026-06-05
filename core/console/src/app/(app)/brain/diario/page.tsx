"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import ReactMarkdown from "react-markdown";

import { LLMPolishBadge } from "@/components/brain/LLMPolishBadge";
import { useBrainContext } from "@/components/brain/useBrainContext";
import { PanelEmpty, PanelLoading } from "@/components/brain/Panels";
import { fetchJournal } from "@/lib/brain/surfaces";
import {
  shortEventRef,
  whatChangedLabel,
} from "@/lib/brain/format";
import { useEventTitles, type EventMeta } from "@/lib/brain/useEventTitles";
import type { JournalEntry } from "@/lib/brain/types";

interface JournalSectionProps {
  title: string;
  items: Array<unknown>;
  renderer?: (item: unknown) => string;
  emptyLabel?: string;
}

function fallbackRender(item: unknown): string {
  if (typeof item === "string") return item;
  if (item && typeof item === "object") {
    const obj = item as { text?: string; title?: string; summary?: string };
    return obj.text ?? obj.title ?? obj.summary ?? "(voce non leggibile)";
  }
  return String(item);
}

function renderWhatChanged(item: unknown): string {
  const bucket = whatChangedLabel(item);
  return bucket ? bucket.label : fallbackRender(item);
}

function makeTitleResolver(
  titles: Record<string, EventMeta>,
): (item: unknown) => string {
  return (item: unknown) => {
    if (typeof item !== "string") return fallbackRender(item);
    const meta = titles[item];
    if (!meta || !meta.title) return `Evento · ${shortEventRef(item)}`;
    const project = meta.source_project ? ` · ${meta.source_project}` : "";
    return `${meta.title}${project}`;
  };
}

function makeDecisionResolver(
  titles: Record<string, EventMeta>,
): (item: unknown) => string {
  return (item: unknown) => {
    if (typeof item !== "string") return "Decisione osservata";
    const meta = titles[item];
    if (!meta || !meta.title) return `Decisione · ${shortEventRef(item)}`;
    const project = meta.source_project ? ` · ${meta.source_project}` : "";
    return `Decisione · ${meta.title}${project}`;
  };
}

function JournalSection({ title, items, renderer = fallbackRender, emptyLabel }: JournalSectionProps) {
  if (!items.length) {
    if (!emptyLabel) return null;
    return (
      <section className="flex flex-col gap-2">
        <h3 className="font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.22em] text-pir-text-tertiary">
          {title}
        </h3>
        <p className="font-[var(--font-exo-2)] text-sm text-pir-text-tertiary">
          {emptyLabel}
        </p>
      </section>
    );
  }
  return (
    <section className="flex flex-col gap-2">
      <h3 className="font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.22em] text-pir-text-tertiary">
        {title}
        <span className="ml-2 text-pir-text-tertiary normal-case tracking-normal">· {items.length}</span>
      </h3>
      <ul className="flex flex-col gap-1.5 font-[var(--font-exo-2)] text-sm text-pir-text-primary">
        {items.slice(0, 50).map((item, idx) => (
          <li key={idx} className="border-l-2 border-pir-border pl-3 text-pir-text-secondary">
            {renderer(item)}
          </li>
        ))}
        {items.length > 50 && (
          <li className="text-pir-text-tertiary text-xs">
            … e altre {items.length - 50} voci omesse.
          </li>
        )}
      </ul>
    </section>
  );
}

export default function BrainDiarioPage() {
  const { cycleKey, scope } = useBrainContext();
  // Wave 3.1 deep-link (Emilio 2026-05-19): URL `?scope=...&key=...` override il
  // context globale per rendere il diario condivisibile come permalink. Senza
  // questo, qualunque URL `/brain/diario?...` mostrava sempre lo scope=company
  // di default e la `?key=` era ignorata.
  const searchParams = useSearchParams();
  const urlScope = (searchParams?.get("scope") ?? "").toLowerCase();
  const urlKey = searchParams?.get("key") ?? null;
  const effectiveScope: "company" | "program" | "project" =
    urlScope === "project" || urlScope === "program" || urlScope === "company"
      ? (urlScope as "company" | "program" | "project")
      : scope;
  const effectiveScopeKey: string | null =
    effectiveScope === "company" ? null : urlKey;
  const [entry, setEntry] = useState<JournalEntry | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    async function load() {
      try {
        const resp = await fetchJournal({
          cycle_key: cycleKey ?? "latest",
          scope_type: effectiveScope,
          scope_key: effectiveScopeKey ?? undefined,
          limit: 1,
        });
        if (!active) return;
        setEntry((resp.items?.[0] as JournalEntry) ?? null);
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [cycleKey, effectiveScope, effectiveScopeKey]);

  // Hooks before any early-return guards (rules-of-hooks).
  const decisionIds: string[] = (() => {
    if (!entry) return [];
    const v = entry as { body?: { decisions_observed?: unknown[]; sources?: unknown[] } };
    const out: string[] = [];
    for (const ev of v.body?.decisions_observed ?? []) {
      if (typeof ev === "string") out.push(ev);
    }
    // Sources can be 800+, sample first 30 — diario titles preview, not full citation list.
    for (const ev of (v.body?.sources ?? []).slice(0, 30)) {
      if (typeof ev === "string") out.push(ev);
    }
    return out;
  })();
  const titles = useEventTitles(decisionIds, cycleKey);

  if (loading) return <PanelLoading message="diario · raccogliendo eventi" />;
  if (!entry) return <PanelEmpty message="Nessun racconto per questo ciclo" />;

  const view = entry as {
    body?: Record<string, unknown[]>;
    scope_type?: string;
    scope_key?: string;
    cycle_key?: string;
    published_at?: string;
    narrative_polished?: string;
    cited_evidence_refs?: string[];
    polish_model?: string;
  };
  const body = view.body ?? {};
  const narrative = view.narrative_polished ?? "";
  const decisionRender = makeDecisionResolver(titles);
  const titleRender = makeTitleResolver(titles);
  // Wave 3.1 UX restructure (Emilio feedback 2026-05-19): la narrazione LLM
  // diventa il contenuto primario (hero markdown). I 6 blocchi struttura
  // (what_changed / decisions / loops / contesto / tomorrow_watch / sources)
  // restano accessibili come "Dettagli evento" collapsible — chiuso default,
  // power-user mode. Fallback senza polish: messaggio "Polish in corso" +
  // struttura grezza per non perdere la vista durante backfill.
  const hasNarrative = Boolean(narrative.trim());
  const hasEvents =
    ((body.what_changed as unknown[]) ?? []).length > 0 ||
    ((body.decisions_observed as unknown[]) ?? []).length > 0 ||
    ((body.sources as unknown[]) ?? []).length > 0;
  return (
    <article
      className="flex flex-col gap-6 border border-pir-border bg-[hsl(var(--pir-surface-1))] p-6"
      style={{ borderRadius: "2px" }}
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-pir-text-tertiary">
        <span>
          Diario · scope {view.scope_type ?? "company"}
          {view.scope_type !== "company" && view.scope_key && (
            <> / <span className="normal-case tracking-normal">{view.scope_key}</span></>
          )} · ciclo {view.cycle_key ?? "—"}
        </span>
        {view.published_at && (
          <span className="normal-case tracking-normal text-pir-text-tertiary">
            pubblicato {new Date(view.published_at).toLocaleString("it-IT")}
          </span>
        )}
      </header>

      {hasNarrative ? (
        <section className="flex flex-col gap-3">
          <div
            className="prose prose-sm max-w-none font-[var(--font-exo-2)] text-base leading-relaxed text-pir-text-primary
                       prose-headings:font-[var(--font-exo-2)] prose-headings:text-pir-text-primary
                       prose-strong:text-pir-text-primary prose-a:text-[hsl(var(--pir-accent))]
                       prose-code:text-pir-text-secondary prose-code:bg-[hsl(var(--pir-surface-2))]
                       prose-code:px-1 prose-code:rounded-sm prose-li:my-1"
          >
            <ReactMarkdown>{narrative}</ReactMarkdown>
          </div>
          <div className="font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-pir-text-tertiary">
            <LLMPolishBadge
              polished
              citedRefs={view.cited_evidence_refs}
              model={view.polish_model}
            />
          </div>
        </section>
      ) : (
        <section
          className="border-l-2 border-pir-border bg-[hsl(var(--pir-surface-2))] p-4"
          style={{ borderRadius: "2px" }}
        >
          <p className="font-[var(--font-exo-2)] text-sm text-pir-text-secondary">
            {hasEvents
              ? "Polish in corso — la narrazione LLM italiana arriva nel prossimo ciclo. Sotto i dettagli grezzi degli eventi."
              : "Nessun racconto né eventi per questo ciclo."}
          </p>
        </section>
      )}

      <details className="group flex flex-col gap-3" open={!hasNarrative && hasEvents}>
        <summary className="flex cursor-pointer items-center justify-between font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.22em] text-pir-text-tertiary hover:text-pir-text-secondary">
          <span>Dettagli evento</span>
          <span className="transition-transform group-open:rotate-90">›</span>
        </summary>
        <div className="mt-3 flex flex-col gap-5 border-t border-pir-border pt-4">
          <JournalSection
            title="Cosa è cambiato"
            items={(body.what_changed as unknown[]) ?? []}
            renderer={renderWhatChanged}
            emptyLabel="Nessun cambio rilevato in questo ciclo."
          />
          <JournalSection
            title="Decisioni osservate"
            items={(body.decisions_observed as unknown[]) ?? []}
            renderer={decisionRender}
          />
          <JournalSection
            title="Loop aperti"
            items={(body.open_loops as unknown[]) ?? []}
          />
          <JournalSection
            title="Contesto rilevante"
            items={(body.notable_context as unknown[]) ?? []}
          />
          <JournalSection
            title="Da osservare domani"
            items={(body.tomorrow_watch as unknown[]) ?? []}
          />
          <JournalSection
            title="Eventi citati"
            items={(body.sources as unknown[]) ?? []}
            renderer={titleRender}
          />
        </div>
      </details>
    </article>
  );
}
