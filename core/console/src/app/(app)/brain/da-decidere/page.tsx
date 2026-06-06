"use client";

import { useEffect, useMemo, useState } from "react";

import { LLMPolishBadge } from "@/components/brain/LLMPolishBadge";
import { useBrainContext } from "@/components/brain/useBrainContext";
import { PanelEmpty, PanelLoading } from "@/components/brain/Panels";
import { fetchFindings, patchFinding } from "@/lib/brain/surfaces";
import { useCanPatch } from "@/lib/brain/useCanPatch";
import { useEventTitles, type EventMeta } from "@/lib/brain/useEventTitles";
import { findingTypeLabel, shortEventRef } from "@/lib/brain/format";
import type { BrainFinding } from "@/lib/brain/types";

const EVENT_REF_RE = /event:([a-f0-9]{8,32})/g;

function extractEventRefs(text: string | null | undefined): string[] {
  if (!text) return [];
  const out: string[] = [];
  for (const m of text.matchAll(EVENT_REF_RE)) out.push(m[1]);
  return out;
}

function explainEventRefs(
  text: string | null | undefined,
  titles: Record<string, EventMeta>,
): string {
  if (!text) return "";
  return text.replace(EVENT_REF_RE, (_match, id: string) => {
    const meta = titles[id];
    if (!meta || !meta.title) return `evento ${shortEventRef(id)}`;
    const project = meta.source_project ? ` (${meta.source_project})` : "";
    return `${meta.title}${project}`;
  });
}

function onPatchedReducer(
  next: BrainFinding,
): (prev: BrainFinding[]) => BrainFinding[] {
  const nextId = (next as { finding_id?: string }).finding_id;
  return (prev) =>
    prev.map((p) =>
      (p as { finding_id?: string }).finding_id === nextId ? next : p,
    );
}

function FindingCard({
  finding,
  canPatch,
  onPatched,
  titles,
}: {
  finding: BrainFinding;
  canPatch: boolean;
  onPatched: (next: BrainFinding) => void;
  titles: Record<string, EventMeta>;
}) {
  const data = finding as {
    finding_id?: string;
    finding_type?: string;
    scope_key?: string;
    confidence?: string;
    title?: string;
    why_now?: string;
    why_now_polished?: string;
    summary?: string;
    summary_polished?: string;
    reasoning_polished?: string;
    suggested_artifact?: string;
    approval_state?: string;
    recency_factor?: number | null;
    regression_of_finding_id?: string | null;
    cited_evidence_refs?: string[];
    polish_model?: string;
  };
  const summaryText = data.summary_polished ?? data.summary ?? "";
  const summaryPolished = Boolean(data.summary_polished);
  const whyNowText = data.why_now_polished ?? data.why_now ?? "";
  const whyNowPolished = Boolean(data.why_now_polished);
  const reasoningText = data.reasoning_polished ?? "";
  const reasoningPolished = Boolean(data.reasoning_polished);
  const [busy, setBusy] = useState(false);
  const recency = typeof data.recency_factor === "number" ? data.recency_factor : null;
  const opacity = recency != null ? Math.max(0.4, recency) : 1;

  async function act(state: "approved" | "dismissed" | "resolved") {
    if (!data.finding_id || busy) return;
    setBusy(true);
    try {
      const next = await patchFinding(data.finding_id, state, null, "console-ui");
      onPatched(next);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      className="flex flex-col gap-2 border border-pir-border bg-[hsl(var(--pir-surface-1))] p-4"
      style={{ borderRadius: "2px", opacity }}
    >
      <header className="flex items-baseline justify-between font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-pir-text-tertiary">
        <span>
          {findingTypeLabel(data.finding_type)} · {data.scope_key ?? "—"}
        </span>
        <span>confidenza · {data.confidence ?? "low"}</span>
      </header>
      <h3 className="font-[var(--font-exo-2)] text-base font-semibold text-pir-text-primary">
        {explainEventRefs(data.title, titles)}
      </h3>
      {summaryText && (
        <p className="font-[var(--font-exo-2)] text-sm text-pir-text-secondary">
          {explainEventRefs(summaryText, titles)}
          <LLMPolishBadge
            polished={summaryPolished}
            citedRefs={data.cited_evidence_refs}
            model={data.polish_model}
          />
        </p>
      )}
      <p className="font-[var(--font-exo-2)] text-sm text-pir-text-secondary">
        Perché ora · {explainEventRefs(whyNowText, titles)}
        <LLMPolishBadge
          polished={whyNowPolished}
          citedRefs={data.cited_evidence_refs}
          model={data.polish_model}
        />
      </p>
      <p className="font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.16em] text-pir-text-tertiary">
        Artefatto suggerito · {data.suggested_artifact ?? "nessuno"} · creazione manuale dopo l'approvazione
      </p>
      {reasoningText && (
        <p className="font-[var(--font-exo-2)] text-sm italic text-pir-text-tertiary">
          {explainEventRefs(reasoningText, titles)}
          <LLMPolishBadge
            polished={reasoningPolished}
            citedRefs={data.cited_evidence_refs}
            model={data.polish_model}
          />
        </p>
      )}
      {data.regression_of_finding_id && (
        <p className="font-[var(--font-jetbrains-mono)] text-[11px] text-[hsl(var(--pir-warning))]">
          regression of {data.regression_of_finding_id}
        </p>
      )}
      {canPatch && data.approval_state === "open" && (
        <div className="flex gap-2 pt-1">
          <button
            type="button"
            onClick={() => act("approved")}
            disabled={busy}
            className="border border-[hsl(var(--pir-success)/0.6)] bg-[hsl(var(--pir-success)/0.1)] px-3 py-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.16em] text-pir-text-primary disabled:opacity-50"
            style={{ borderRadius: "2px" }}
          >
            Approva
          </button>
          <button
            type="button"
            onClick={() => act("dismissed")}
            disabled={busy}
            className="border border-pir-border px-3 py-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.16em] text-pir-text-secondary disabled:opacity-50"
            style={{ borderRadius: "2px" }}
          >
            Dismiss
          </button>
        </div>
      )}
      {!canPatch && data.approval_state === "open" && (
        <p className="font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.18em] text-pir-text-muted">
          read-only · serve ruolo operator per agire
        </p>
      )}
    </article>
  );
}

export default function BrainDaDecidereePage() {
  const { cycleKey, scope } = useBrainContext();
  const [findings, setFindings] = useState<BrainFinding[]>([]);
  const [loading, setLoading] = useState(true);
  const canPatch = useCanPatch();

  useEffect(() => {
    let active = true;
    setLoading(true);
    async function load() {
      try {
        const resp = await fetchFindings({
          cycle_key: cycleKey ?? "latest",
          approval_state: ["open"],
        });
        if (!active) return;
        setFindings(resp.items as BrainFinding[]);
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [cycleKey, scope]);

  const eventIds = useMemo(() => {
    const out: string[] = [];
    for (const f of findings) {
      const data = f as {
        title?: string;
        summary?: string;
        summary_polished?: string;
        why_now?: string;
        why_now_polished?: string;
        reasoning_polished?: string;
      };
      for (const t of [
        data.title,
        data.summary,
        data.summary_polished,
        data.why_now,
        data.why_now_polished,
        data.reasoning_polished,
      ]) {
        if (t) out.push(...extractEventRefs(t));
      }
    }
    return out;
  }, [findings]);
  const titles = useEventTitles(eventIds, cycleKey);

  if (loading) return <PanelLoading message="learn · costruisco proposte approvabili" />;
  if (findings.length === 0) return <PanelEmpty message="Nessuna proposta pendente in questo ciclo" />;
  return (
    <div className="flex flex-col gap-3">
      {findings.map((f, idx) => (
        <FindingCard
          key={(f as { finding_id?: string }).finding_id ?? `finding-${idx}`}
          finding={f}
          canPatch={canPatch}
          onPatched={(next) => setFindings(onPatchedReducer(next))}
          titles={titles}
        />
      ))}
    </div>
  );
}
