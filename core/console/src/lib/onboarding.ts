export const ONBOARDED_STORAGE_KEY = "marvis:onboarded";
export const DEMO_SEEDED_STORAGE_KEY = "marvis:demo-seeded";

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function truthyStorageValue(value: string | null): boolean {
  return value === "1" || value === "true";
}

export function shouldShowOnboarding(storage: StorageLike): boolean {
  return !truthyStorageValue(storage.getItem(ONBOARDED_STORAGE_KEY)) &&
    !truthyStorageValue(storage.getItem(DEMO_SEEDED_STORAGE_KEY));
}

export function markOnboardingDone(storage: StorageLike): void {
  storage.setItem(ONBOARDED_STORAGE_KEY, "true");
}

export function markDemoSeeded(storage: StorageLike): void {
  storage.setItem(DEMO_SEEDED_STORAGE_KEY, "true");
}

export function markDemoRemoved(storage: StorageLike): void {
  storage.removeItem(DEMO_SEEDED_STORAGE_KEY);
}

export function parseExclusions(input: string): string[] {
  return input
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export interface SetupDraft {
  operator: string;
  company: string;
  root: string;
  sources: string[];
  exclusions: string[];
  docsConsent: boolean;
  repoConsent: boolean;
  cycleHour: string;
}

export function identitySetupContent(draft: Pick<SetupDraft, "operator" | "company">): string {
  return [
    `operatore: ${draft.operator.trim() || "-"}`,
    `azienda: ${draft.company.trim() || "-"}`,
  ].join("\n");
}

export function sourcesSetupContent(draft: Pick<SetupDraft, "root" | "sources" | "exclusions">): string {
  const sourceLines = draft.sources.length
    ? draft.sources.map((source) => `- ${source}`)
    : ["- nessuna cartella confermata"];
  const exclusionLines = draft.exclusions.length
    ? draft.exclusions.map((exclusion) => `- ${exclusion}`)
    : ["- nessuna esclusione esplicita"];

  return [
    `root: ${draft.root.trim() || "-"}`,
    "indicizza:",
    ...sourceLines,
    "escludi:",
    ...exclusionLines,
  ].join("\n");
}

export function rhythmSetupContent(draft: Pick<SetupDraft, "cycleHour">): string {
  return [
    `ciclo_brain: ${draft.cycleHour || "03:00"}`,
    "giorni_attivi: lun, mar, mer, gio, ven",
    "briefing: pronto dopo il ciclo brain",
  ].join("\n");
}

export function brainSourcesSetupContent(
  draft: Pick<SetupDraft, "docsConsent" | "repoConsent">,
): string {
  const enabled = [
    draft.docsConsent ? "documenti" : null,
    draft.repoConsent ? "repo" : null,
  ].filter(Boolean);
  return [
    `locali: ${enabled.length ? enabled.join(", ") : "nessuna fonte attiva"}`,
    "enterprise_previste: email, knowledge base, gestionale",
  ].join("\n");
}

export function agentPrompt(draft: Pick<SetupDraft, "root" | "sources" | "exclusions">): string {
  const sourceLines = draft.sources.length
    ? draft.sources.map((source) => `- ${source}`).join("\n")
    : "- nessuna cartella confermata";
  const exclusionLines = draft.exclusions.length
    ? draft.exclusions.map((exclusion) => `- ${exclusion}`).join("\n")
    : "- nessuna esclusione esplicita";

  return [
    "marvis init",
    "",
    "Leggi setup.md via MCP Marvis.",
    `Root confermata: ${draft.root || "-"}`,
    "Cartelle da arricchire:",
    sourceLines,
    "Esclusioni esplicite:",
    exclusionLines,
    "Deriva progetti, programmi, relazioni e stato nella directory .marvis/. Non scrivere stato derivato in setup.md.",
  ].join("\n");
}
