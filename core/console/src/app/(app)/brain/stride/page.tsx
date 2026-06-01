"use client";

import { useEffect, useState } from "react";

import { useBrainContext } from "@/components/brain/useBrainContext";
import { KnowledgeGlyph } from "@/components/brain/KnowledgeGlyph";
import { PanelEmpty, PanelLoading } from "@/components/brain/Panels";
import { fetchDrift } from "@/lib/brain/surfaces";
import {
  knowledgeFormLabel,
  severityLabel,
  signalNarrative,
} from "@/lib/brain/format";
import type { DriftSignal } from "@/lib/brain/types";

const AXIS_IT: Record<string, string> = {
  all: "tutti",
  intent: "intento",
  context: "contesto",
  both: "entrambi",
};

type Axis = "intent" | "context" | "both";

const AXES: Axis[] = ["intent", "context", "both"];

function axisButtonClass(active: boolean): string {
  return active
    ? "border-[hsl(var(--pir-accent))] bg-[hsl(var(--pir-accent)/0.12)] text-pir-text-primary"
    : "border-pir-border text-pir-text-secondary hover:text-pir-text-primary";
}

function DriftList({ loading, signals }: { loading: boolean; signals: DriftSignal[] }) {
  if (loading) {
    return <PanelLoading message="drift checker · classifica forme di conoscenza" />;
  }
  if (signals.length === 0) {
    return <PanelEmpty message="Nessun segnale di drift in questo ciclo." />;
  }
  return (
    <div className="flex flex-col gap-3">
      {signals.map((s, idx) => (
        <DriftCard
          key={(s as { signal_id?: string }).signal_id ?? `signal-${idx}`}
          signal={s}
        />
      ))}
    </div>
  );
}

function DriftCard({ signal }: { signal: DriftSignal }) {
  const sig = signal as {
    signal_id?: string;
    severity?: string;
    signal_type?: string;
    observed_delta?: string;
    expected_direction_ref?: string;
    knowledge_form?: string;
    scope_key?: string;
    drift_axis?: string | null;
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
      className="flex flex-col gap-2 border border-pir-border bg-[hsl(var(--pir-surface-1))] p-4"
      style={{ borderRadius: "2px" }}
    >
      <header className="flex items-baseline justify-between font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-pir-text-tertiary">
        <span>
          gravità <span className={sev.toneClass}>{sev.text}</span>
        </span>
        <span className="flex items-center gap-2">
          <span className="normal-case tracking-normal">{knowledgeFormLabel(sig.knowledge_form)}</span>
          <KnowledgeGlyph form={sig.knowledge_form} />
        </span>
      </header>
      <p className="font-[var(--font-exo-2)] text-base text-pir-text-primary">
        {headline}
      </p>
      {sig.observed_delta && (
        <p className="font-[var(--font-jetbrains-mono)] text-[11px] text-pir-text-secondary">
          osservato · {sig.observed_delta}
        </p>
      )}
      {sig.expected_direction_ref && (
        <p className="font-[var(--font-jetbrains-mono)] text-[11px] text-pir-text-secondary">
          atteso · {sig.expected_direction_ref}
        </p>
      )}
      <footer className="flex items-baseline justify-between font-[var(--font-jetbrains-mono)] text-[11px] text-pir-text-tertiary">
        <span>scope · {sig.scope_key ?? "—"}</span>
        {sig.drift_axis && <span>asse · {AXIS_IT[sig.drift_axis] ?? sig.drift_axis}</span>}
      </footer>
    </article>
  );
}

export default function BrainStridePage() {
  const { cycleKey, scope } = useBrainContext();
  const [axis, setAxis] = useState<Axis | "all">("all");
  const [signals, setSignals] = useState<DriftSignal[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    async function load() {
      try {
        // NOTE v1: no `scope_type` filter — DR rules emit signals on the
        // scope where the underlying event lives (mostly 'project'), so
        // a strict company-scope filter would hide everything. The scope
        // toggle in v1 is a semantic chip; per-scope filtering ships in
        // v1.2 with the scope_key picker.
        const resp = await fetchDrift({
          cycle_key: cycleKey ?? "latest",
          state: ["open"],
          severity_min: "low",
          drift_axis: axis === "all" ? undefined : [axis],
          limit: 50,
        });
        if (!active) return;
        setSignals(resp.items as DriftSignal[]);
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [cycleKey, axis]);

  return (
    <div className="flex flex-col gap-4">
      <div
        role="tablist"
        aria-label="Drift axis filter"
        className="flex gap-1 font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em]"
      >
        {(["all", ...AXES] as const).map((opt) => (
          <button
            key={opt}
            role="tab"
            aria-selected={axis === opt}
            type="button"
            onClick={() => setAxis(opt as Axis | "all")}
            className={`border px-3 py-1 ${axisButtonClass(axis === opt)}`}
            style={{ borderRadius: "2px" }}
          >
            {AXIS_IT[opt] ?? opt}
          </button>
        ))}
      </div>

      <DriftList loading={loading} signals={signals} />
    </div>
  );
}
