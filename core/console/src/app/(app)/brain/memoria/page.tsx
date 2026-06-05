"use client";

import { useEffect, useMemo, useState } from "react";

import { useBrainContext } from "@/components/brain/useBrainContext";
import { PanelEmpty, PanelLoading } from "@/components/brain/Panels";
import { fetchMemoryOps, patchMemoryOp } from "@/lib/brain/surfaces";
import { useCanPatch } from "@/lib/brain/useCanPatch";
import { useEventTitles, type EventMeta } from "@/lib/brain/useEventTitles";
import { operationTypeLabel, shortEventRef } from "@/lib/brain/format";
import type { MemoryOperation } from "@/lib/brain/types";

const EVENT_REF_RE = /event:([a-f0-9]{8,32})/g;

/** Extract event_id references from a free-text summary or body field. */
function extractEventRefs(text: string | null | undefined): string[] {
  if (!text) return [];
  const out: string[] = [];
  for (const m of text.matchAll(EVENT_REF_RE)) {
    out.push(m[1]);
  }
  return out;
}

function describeEvent(meta: EventMeta | undefined, fallbackId: string): string {
  if (!meta) return `evento ${shortEventRef(fallbackId)}`;
  const t = meta.title?.trim();
  if (!t) return `evento ${shortEventRef(fallbackId)}`;
  const project = meta.source_project ? ` (${meta.source_project})` : "";
  return `${t}${project}`;
}

/** Rewrite "event:xyz vs event:abc" using real titles when available. */
function explainContradictionSummary(
  summary: string | null | undefined,
  titles: Record<string, EventMeta>,
): string {
  if (!summary) return "";
  return summary.replace(EVENT_REF_RE, (_match, id: string) =>
    describeEvent(titles[id], id),
  );
}

function memoryReducer(
  next: MemoryOperation,
): (prev: MemoryOperation[]) => MemoryOperation[] {
  const nextId = (next as { operation_id?: string }).operation_id;
  return (prev) =>
    prev.map((p) =>
      (p as { operation_id?: string }).operation_id === nextId ? next : p,
    );
}

function MemoryCard({
  op,
  canPatch,
  onPatched,
  titles,
}: {
  op: MemoryOperation;
  canPatch: boolean;
  onPatched: (next: MemoryOperation) => void;
  titles: Record<string, EventMeta>;
}) {
  const data = op as {
    operation_id?: string;
    operation_type?: string;
    score?: number;
    recurrence_count?: number;
    summary?: string;
    proposed_write?: { target_type?: string };
    approval_state?: string;
  };
  const [busy, setBusy] = useState(false);

  async function act(state: "approved" | "dismissed" | "rejected") {
    if (!data.operation_id || busy) return;
    setBusy(true);
    try {
      const next = await patchMemoryOp(data.operation_id, state, null, "console-ui");
      onPatched(next);
    } finally {
      setBusy(false);
    }
  }

  const summaryExplained = explainContradictionSummary(data.summary, titles);

  return (
    <article
      className="flex flex-col gap-2 border border-pir-border bg-[hsl(var(--pir-surface-1))] p-4"
      style={{ borderRadius: "2px" }}
    >
      <header className="flex items-baseline justify-between font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.18em] text-pir-text-tertiary">
        <span>{operationTypeLabel(data.operation_type)}</span>
        <span>
          score{" "}
          {typeof data.score === "number" ? data.score.toFixed(2) : "—"} ·{" "}
          ricorre {data.recurrence_count ?? "—"}
        </span>
      </header>
      <p className="font-[var(--font-exo-2)] text-sm text-pir-text-primary">
        {summaryExplained || "(senza descrizione)"}
      </p>
      <p className="font-[var(--font-jetbrains-mono)] text-[11px] text-pir-text-secondary">
        propone · {data.proposed_write?.target_type ?? "nessuna scrittura"}
      </p>
      {canPatch && data.approval_state === "pending" && (
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
      {!canPatch && data.approval_state === "pending" && (
        <p className="font-[var(--font-jetbrains-mono)] text-[10px] uppercase tracking-[0.18em] text-pir-text-muted">
          read-only · serve ruolo operator per agire
        </p>
      )}
    </article>
  );
}

export default function BrainMemoriaPage() {
  const { cycleKey, scope } = useBrainContext();
  const [ops, setOps] = useState<MemoryOperation[]>([]);
  const [loading, setLoading] = useState(true);
  const canPatch = useCanPatch();

  useEffect(() => {
    let active = true;
    setLoading(true);
    async function load() {
      try {
        const resp = await fetchMemoryOps({
          cycle_key: cycleKey ?? "latest",
          approval_state: ["pending"],
        });
        if (!active) return;
        setOps(resp.items as MemoryOperation[]);
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [cycleKey, scope]);

  const grouped = useMemo(() => {
    const buckets = new Map<string, MemoryOperation[]>();
    for (const op of ops) {
      const t = (op as { operation_type?: string }).operation_type ?? "other";
      if (!buckets.has(t)) buckets.set(t, []);
      buckets.get(t)!.push(op);
    }
    return Array.from(buckets.entries());
  }, [ops]);

  const eventIds = useMemo(() => {
    const out: string[] = [];
    for (const op of ops) {
      out.push(
        ...extractEventRefs(
          (op as { summary?: string | null }).summary ?? "",
        ),
      );
    }
    return out;
  }, [ops]);
  const titles = useEventTitles(eventIds, cycleKey);

  if (loading) return <PanelLoading message="memory ops · cerco connessioni da rinforzare" />;
  if (ops.length === 0) return <PanelEmpty message="Nessuna operazione di memoria in questo ciclo" />;
  return (
    <div className="flex flex-col gap-5">
      {grouped.map(([type, items]) => (
        <section key={type} className="flex flex-col gap-3">
          <h2 className="font-[var(--font-jetbrains-mono)] text-[11px] uppercase tracking-[0.22em] text-pir-text-tertiary">
            {type.replace(/_/g, " ")}
          </h2>
          <div className="flex flex-col gap-2">
            {items.map((op, idx) => (
              <MemoryCard
                key={(op as { operation_id?: string }).operation_id ?? `op-${idx}`}
                op={op}
                canPatch={canPatch}
                onPatched={(next) => setOps(memoryReducer(next))}
                titles={titles}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
