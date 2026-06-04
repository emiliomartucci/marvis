// MarvisX Console — Brain v1 human-readable formatting helpers (sub-05).
// Pure functions, no fetch. Maps technical brain payloads (event_id, signal_type,
// finding_type, knowledge_form) to founder-friendly Italian labels.

// ----------------------------------------------------------------------------
// Italian labels for technical taxonomies
// ----------------------------------------------------------------------------

const SIGNAL_TYPE_IT: Record<string, string> = {
  activity_without_status: "Attività senza aggiornamento status",
  decision_without_adr: "Decisione senza ADR/plan linkato",
  playbook_changed: "Procedura cambiata",
  stale_open_loop: "Loop aperto ricorrente",
  docs_governance_drift: "Documento stantio",
  external_update_unpropagated: "Novità esterna non propagata",
  claimed_decision_gap: "Decisione dichiarata senza traccia audit",
  plan_implementation_gap: "Piano non implementato",
};

const FINDING_TYPE_IT: Record<string, string> = {
  idea: "Idea",
  task_candidate: "Candidato a diventare task",
  open_question: "Domanda aperta",
  scope_gap: "Gap di scope",
  procedure_change: "Cambio procedura",
  contradiction: "Contraddizione",
  plan_gap: "Gap implementazione piano",
};

const OPERATION_TYPE_IT: Record<string, string> = {
  consolidate: "Consolida duplicati",
  reinforce: "Rinforza percorso",
  harden_provenance: "Indurisci provenienza",
  dedup: "Rimuovi duplicati",
  distill: "Distilla pattern",
  cascade: "Propaga a cascata",
  orphan_detected: "File orfano rilevato",
  contradiction_detected: "Contraddizione rilevata",
};

const KNOWLEDGE_FORM_IT: Record<string, string> = {
  adr: "ADR (decisione architetturale)",
  spec: "Specifica",
  playbook: "Playbook",
  tribal_memory: "Memoria orale",
  external_update: "Update esterno",
  claimed_decision: "Decisione dichiarata",
  unknown: "Da classificare",
};

const SEVERITY_IT: Record<string, string> = {
  low: "bassa",
  medium: "media",
  high: "alta",
  critical: "critica",
};

const SEVERITY_TONE: Record<string, string> = {
  low: "text-pir-text-tertiary",
  medium: "text-[hsl(var(--pir-warning))]",
  high: "text-[hsl(var(--pir-accent))]",
  critical: "text-[hsl(var(--pir-error))]",
};

const DOMAIN_IT: Record<string, string> = {
  task: "task",
  pr: "pull request",
  commit: "commit",
  handoff: "handoff",
  learning: "learning",
  doc: "documento",
  ingest: "ingest",
  kg: "knowledge graph",
  external: "esterno",
  regression: "regressione",
  file: "file",
};

// ----------------------------------------------------------------------------
// Public formatters
// ----------------------------------------------------------------------------

/** Italian human label for a drift signal type. */
export function signalTypeLabel(signalType: string | null | undefined): string {
  if (!signalType) return "Segnale";
  return SIGNAL_TYPE_IT[signalType] ?? signalType.replace(/_/g, " ");
}

/** Italian human label for a finding type. */
export function findingTypeLabel(findingType: string | null | undefined): string {
  if (!findingType) return "Finding";
  return FINDING_TYPE_IT[findingType] ?? findingType.replace(/_/g, " ");
}

/** Italian human label for a memory operation type. */
export function operationTypeLabel(opType: string | null | undefined): string {
  if (!opType) return "Operazione";
  return OPERATION_TYPE_IT[opType] ?? opType.replace(/_/g, " ");
}

/** Italian human label for a knowledge form. */
export function knowledgeFormLabel(form: string | null | undefined): string {
  if (!form) return "—";
  return KNOWLEDGE_FORM_IT[form] ?? form.replace(/_/g, " ");
}

/** Italian severity word + Tailwind color class. */
export function severityLabel(severity: string | null | undefined): {
  text: string;
  toneClass: string;
} {
  const key = (severity ?? "low").toLowerCase();
  return {
    text: SEVERITY_IT[key] ?? key,
    toneClass: SEVERITY_TONE[key] ?? SEVERITY_TONE.low,
  };
}

/** "12 eventi nel dominio commit" — used to render a what_changed bucket. */
export function whatChangedLabel(
  item: unknown,
): { domain: string; count: number; label: string } | null {
  if (!item || typeof item !== "object") return null;
  const obj = item as { domain?: unknown; event_ids?: unknown };
  const domain = typeof obj.domain === "string" ? obj.domain : null;
  const ids = Array.isArray(obj.event_ids) ? obj.event_ids : [];
  if (!domain) return null;
  const niceDomain = DOMAIN_IT[domain] ?? domain;
  const count = ids.length;
  const noun = count === 1 ? "evento" : "eventi";
  return {
    domain: niceDomain,
    count,
    label: `${count} ${noun} · ${niceDomain}`,
  };
}

/** Short readable event reference (event_id is BLAKE2b-16 hex, 32 chars). */
export function shortEventRef(eventId: string | null | undefined): string {
  if (!eventId) return "—";
  const compact = eventId.replace(/^[a-z]+:/, "");
  return compact.length > 8 ? `${compact.slice(0, 8)}…` : compact;
}

/** Italian narrative for a single drift signal observed_delta. */
export function signalNarrative(signal: {
  signal_type?: string | null;
  observed_delta?: string | null;
  scope_key?: string | null;
  knowledge_form?: string | null;
}): string {
  const typeIt = signalTypeLabel(signal.signal_type);
  const scope = signal.scope_key && signal.scope_key !== "__company__" ? signal.scope_key : null;
  if (scope) {
    return `${typeIt} su ${scope}.`;
  }
  return `${typeIt}.`;
}

/** Italian narrative for a finding title (covers DR-generated generic titles). */
export function findingNarrative(finding: {
  finding_type?: string | null;
  title?: string | null;
  why_now?: string | null;
  scope_key?: string | null;
}): { headline: string; detail: string } {
  const typeIt = findingTypeLabel(finding.finding_type);
  const scope = finding.scope_key && finding.scope_key !== "__company__" ? finding.scope_key : null;
  // The DR2 rule emits titles like "Decision without ADR on event:abc...".
  // Re-frame in Italian; keep the original technical title as the detail line
  // for forensic traceability.
  const headline = scope
    ? `${typeIt} · ${scope}`
    : typeIt;
  const detail = finding.title ?? finding.why_now ?? "";
  return { headline, detail };
}

/** Decisions_observed entries are event_id strings — surface as "Decisione X". */
export function decisionRefLabel(eventId: unknown): string {
  if (typeof eventId !== "string") return "Decisione osservata";
  return `Decisione · ${shortEventRef(eventId)}`;
}
