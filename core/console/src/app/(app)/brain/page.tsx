"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

import { LLMPolishBadge } from "@/components/brain/LLMPolishBadge";
import { useBrainContext } from "@/components/brain/useBrainContext";
import { KnowledgeGlyph } from "@/components/brain/KnowledgeGlyph";
import { PanelEmpty, PanelLoading } from "@/components/brain/Panels";
import {
  fetchCycleRecap,
  fetchDrift,
  fetchFindings,
  fetchJournal,
  type CycleRecap,
  type CycleRecapProjectBlock,
} from "@/lib/brain/surfaces";
import {
  findingNarrative,
  severityLabel,
  signalNarrative,
  whatChangedLabel,
} from "@/lib/brain/format";
import type {
  BrainFinding,
  DriftSignal,
  JournalEntry,
} from "@/lib/brain/types";

interface DailyBlocks {
  findings: BrainFinding[];
  drift: DriftSignal[];
  journal: JournalEntry | null;
  recap: CycleRecap | null;
}

function DailyBlock({
  title,
  count,
  href,
  children,
}: {
  title: string;
  count: number | null;
  href: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className="border border-pir-border bg-[hsl(var(--pir-surface-1))] p-4"
      style={{ borderRadius: "2px" }}
    >
      <header className="mb-3 flex items-baseline justify-between">
        <h2 className="font-[var(--font-exo-2)] text-sm font-semibold uppercase tracking-[0.22em] text-pir-text-primary">
          {title}
          {count !== null && (
            <span className="ml-2 text-pir-text-tertiary">· {count}</span>
          )}
        </h2>
        <a
          href={href}
          className="font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-[hsl(var(--pir-accent))] hover:underline"
        >
          Apri tutti →
        </a>
      </header>
      {children}
    </section>
  );
}

function FindingMini({ finding }: { finding: BrainFinding }) {
  const view = finding as {
    finding_type?: string;
    scope_key?: string;
    confidence?: string;
    title?: string;
    why_now?: string;
    why_now_polished?: string;
    cited_evidence_refs?: string[];
    polish_model?: string;
  };
  const whyNow = view.why_now_polished ?? view.why_now ?? "";
  const whyNowPolished = Boolean(view.why_now_polished);
  const narrative = findingNarrative({
    finding_type: view.finding_type,
    title: view.title,
    why_now: view.why_now,
    scope_key: view.scope_key,
  });
  const confidence = view.confidence ?? "low";
  return (
    <article
      className="border border-pir-border bg-[hsl(var(--pir-surface-2))] px-3 py-2"
      style={{ borderRadius: "2px" }}
    >
      <header className="mb-1 flex items-baseline justify-between font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.14em] text-pir-text-tertiary">
        <span>{narrative.headline}</span>
        <span>confidenza {confidence}</span>
      </header>
      {whyNow && (
        <p className="mt-1 font-[var(--font-exo-2)] text-xs text-pir-text-secondary">
          Perché ora · {whyNow}
          <LLMPolishBadge
            polished={whyNowPolished}
            citedRefs={view.cited_evidence_refs}
            model={view.polish_model}
          />
        </p>
      )}
      {narrative.detail && (
        <p className="mt-1 font-[var(--font-jetbrains-mono)] text-[10px] text-pir-text-tertiary">
          rif · {narrative.detail.length > 90 ? `${narrative.detail.slice(0, 90)}…` : narrative.detail}
        </p>
      )}
    </article>
  );
}

function DriftMini({ signal }: { signal: DriftSignal }) {
  const sig = signal as {
    severity?: string;
    signal_type?: string;
    observed_delta?: string;
    knowledge_form?: string;
    scope_key?: string;
  };
  const sev = severityLabel(sig.severity);
  const headline = signalNarrative({
    signal_type: sig.signal_type,
    observed_delta: sig.observed_delta,
    scope_key: sig.scope_key,
    knowledge_form: sig.knowledge_form,
  });
  return (
    <article
      className="border border-pir-border bg-[hsl(var(--pir-surface-2))] px-3 py-2"
      style={{ borderRadius: "2px" }}
    >
      <header className="mb-1 flex items-baseline justify-between font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.14em] text-pir-text-tertiary">
        <span>
          gravità <span className={sev.toneClass}>{sev.text}</span>
        </span>
        <KnowledgeGlyph form={sig.knowledge_form} />
      </header>
      <p className="font-[var(--font-exo-2)] text-sm text-pir-text-primary">
        {headline}
      </p>
      {sig.observed_delta && (
        <p className="mt-1 font-[var(--font-jetbrains-mono)] text-[10px] text-pir-text-tertiary">
          dettaglio · {sig.observed_delta.length > 100 ? `${sig.observed_delta.slice(0, 100)}…` : sig.observed_delta}
        </p>
      )}
    </article>
  );
}

function JournalPreview({ entry }: { entry: JournalEntry | null }) {
  if (!entry) return <PanelEmpty message="Nessun racconto per oggi" />;
  const view = entry as {
    body?: { what_changed?: unknown[]; decisions_observed?: unknown[]; sources?: unknown[] };
    narrative_polished?: string;
    cited_evidence_refs?: string[];
    polish_model?: string;
  };
  const body = view.body ?? {};
  const whatChanged = (body.what_changed ?? []) as unknown[];
  const decisions = (body.decisions_observed ?? []) as unknown[];
  const totalSources = ((body.sources ?? []) as unknown[]).length;
  const narrative = view.narrative_polished ?? "";
  const buckets = whatChanged
    .map(whatChangedLabel)
    .filter((b): b is { domain: string; count: number; label: string } => b !== null)
    .sort((a, b) => b.count - a.count);
  // Wave 3.1 UX restructure (Emilio feedback 2026-05-19): narrative LLM
  // primary content, KPI counters secondary. Markdown render via
  // react-markdown coerente con /diario hero. Fallback "Polish in corso"
  // se narrative IS NULL ma eventi presenti (cohort backfill).
  const hasNarrative = Boolean(narrative.trim());
  return (
    <div className="flex flex-col gap-4">
      {hasNarrative ? (
        <section className="flex flex-col gap-2">
          <div
            className="prose prose-sm max-w-none font-[var(--font-exo-2)] text-[15px] leading-relaxed text-pir-text-primary
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
        <p className="font-[var(--font-exo-2)] text-sm text-pir-text-secondary">
          Polish in corso — la narrazione LLM arriva nel prossimo ciclo. Sotto i
          KPI grezzi del ciclo.
        </p>
      )}
      {totalSources > 0 && (
        <p className="font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-pir-text-tertiary">
          {totalSources} eventi totali · {decisions.length} decisioni osservate
        </p>
      )}
      <details className="group flex flex-col gap-2">
        <summary className="flex cursor-pointer items-center justify-between font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.22em] text-pir-text-tertiary hover:text-pir-text-secondary">
          <span>Dettagli evento · {buckets.length} domini</span>
          <span className="transition-transform group-open:rotate-90">›</span>
        </summary>
        {buckets.length > 0 ? (
          <ul className="mt-2 flex flex-col gap-1 border-t border-pir-border pt-2 font-[var(--font-exo-2)] text-sm text-pir-text-primary">
            {buckets.slice(0, 6).map((b) => (
              <li
                key={b.domain}
                className="border-l-2 border-pir-border pl-3 text-pir-text-secondary"
              >
                {b.label}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-pir-text-tertiary text-sm">Nessuna voce rilevante.</p>
        )}
      </details>
    </div>
  );
}

function HeroRecap({ recap }: { recap: CycleRecap | null }) {
  if (!recap) return null;
  return (
    <section
      className="border-l-4 border-[hsl(var(--pir-accent))] bg-[hsl(var(--pir-surface-1))] p-5"
      style={{ borderRadius: "2px" }}
    >
      <header className="mb-2 font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.22em] text-pir-text-tertiary">
        Sintesi del ciclo · {recap.resolved_cycle_key ?? recap.cycle_key}
      </header>
      <p className="font-[var(--font-exo-2)] text-xl leading-snug text-pir-text-primary">
        {recap.company.narrative}
      </p>
    </section>
  );
}

function ProjectRecapCard({ block }: { block: CycleRecapProjectBlock }) {
  return (
    <article
      className="border border-pir-border bg-[hsl(var(--pir-surface-2))] p-3"
      style={{ borderRadius: "2px" }}
    >
      <header className="mb-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-pir-text-tertiary">
        {block.scope_key}
      </header>
      <p className="font-[var(--font-exo-2)] text-sm text-pir-text-primary">
        {block.narrative}
      </p>
      {block.decisions.length > 0 && (
        <ul className="mt-2 flex flex-col gap-0.5 font-[var(--font-exo-2)] text-xs text-pir-text-secondary">
          {block.decisions.slice(0, 3).map((d) => (
            <li key={d.event_id} className="border-l-2 border-pir-border pl-2">
              <span className="text-pir-text-tertiary">decisione · </span>
              {d.title.length > 80 ? `${d.title.slice(0, 80)}…` : d.title}
            </li>
          ))}
          {block.decisions_count > 3 && (
            <li className="text-pir-text-tertiary text-[11px]">
              … e altre {block.decisions_count - 3} decisioni.
            </li>
          )}
        </ul>
      )}
    </article>
  );
}

export default function BrainDailyPage() {
  const { cycleKey, refreshing } = useBrainContext();
  const [data, setData] = useState<DailyBlocks | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    async function load() {
      try {
        const [findings, drift, journal, recap] = await Promise.all([
          fetchFindings({ cycle_key: cycleKey ?? "latest", approval_state: ["open"], limit: 5 }),
          fetchDrift({ cycle_key: cycleKey ?? "latest", state: ["open"], severity_min: "medium", limit: 5 }),
          fetchJournal({ cycle_key: cycleKey ?? "latest", scope_type: "company", limit: 1 }),
          fetchCycleRecap(cycleKey ?? "latest").catch(() => null),
        ]);
        if (!active) return;
        setData({
          findings: findings.items as BrainFinding[],
          drift: drift.items as DriftSignal[],
          journal: (journal.items?.[0] as JournalEntry) ?? null,
          recap,
        });
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [cycleKey]);

  if (loading || refreshing) {
    return <PanelLoading message="raccogliendo eventi · digest" />;
  }
  if (!data) {
    return <PanelEmpty message="Nessun ciclo disponibile" />;
  }
  const projects = data.recap?.projects ?? [];
  return (
    <div className="flex flex-col gap-4">
      <HeroRecap recap={data.recap} />
      {projects.length > 0 && (
        <section
          className="border border-pir-border bg-[hsl(var(--pir-surface-1))] p-4"
          style={{ borderRadius: "2px" }}
        >
          <header className="mb-3 font-[var(--font-exo-2)] text-sm font-semibold uppercase tracking-[0.22em] text-pir-text-primary">
            Per progetto
            <span className="ml-2 text-pir-text-tertiary">· {projects.length}</span>
          </header>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {projects.slice(0, 12).map((p) => (
              <ProjectRecapCard key={p.scope_key} block={p} />
            ))}
          </div>
          {projects.length > 12 && (
            <p className="mt-2 font-[var(--font-jetbrains-mono)] text-[11px] text-pir-text-tertiary">
              … e altri {projects.length - 12} progetti meno attivi.
            </p>
          )}
        </section>
      )}
      <DailyBlock title="Da decidere" count={data.findings.length} href="/brain/da-decidere/">
        {data.findings.length === 0 ? (
          <PanelEmpty message="Nessun finding pendente" />
        ) : (
          <div className="flex flex-col gap-2">
            {data.findings.map((f, idx) => (
              <FindingMini
                key={(f as { finding_id?: string }).finding_id ?? `finding-${idx}`}
                finding={f}
              />
            ))}
          </div>
        )}
      </DailyBlock>
      <DailyBlock title="Stride" count={data.drift.length} href="/brain/stride/">
        {data.drift.length === 0 ? (
          <PanelEmpty message="Nessun drift signal oggi" />
        ) : (
          <div className="flex flex-col gap-2">
            {data.drift.map((s, idx) => (
              <DriftMini
                key={(s as { signal_id?: string }).signal_id ?? `drift-${idx}`}
                signal={s}
              />
            ))}
          </div>
        )}
      </DailyBlock>
      <DailyBlock title="Diario · oggi" count={null} href="/brain/diario/">
        <JournalPreview entry={data.journal} />
      </DailyBlock>
    </div>
  );
}
