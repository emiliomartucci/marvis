"use client";

import { useState, type ReactNode } from "react";
import type { IngestPendingItem } from "@/lib/types";
import { APIError, deleteIngestPending, patchIngestPending } from "@/lib/api";
import ProjectSelectorModal from "@/components/ProjectSelectorModal";
import { basename, fileLabel, formatBytes, formatStatus, previewKind, statusTone } from "./format";
import { MimeIcon } from "./MimeIcon";
import { MarkdownPreview } from "./preview/MarkdownPreview";
import { PdfPreview } from "./preview/PdfPreview";
import { XlsxPreview } from "./preview/XlsxPreview";
import { ImagePreview } from "./preview/ImagePreview";
import { ParseErrorBanner } from "./states/ParseErrorBanner";

const TABS = ["preview", "extract", "proposal", "saga", "raw"] as const;

const PROJECT_CHANGE_ALLOWED_STATES = new Set<IngestPendingItem["status"]>([
  "awaiting_triage",
  "parse_error",
  "rejected",
]);

type InspectorTab = (typeof TABS)[number];

type ItemState =
  | { status: "idle" }
  | { status: "approving" }
  | { status: "rejecting" }
  | { status: "retrying" }
  | { status: "moving" }
  | { status: "deleting" }
  | { status: "error"; error: string };

interface PendingDetailProps {
  item: IngestPendingItem;
  previewText: string;
  previewLoading?: boolean;
  onApprove: (item: IngestPendingItem) => Promise<void>;
  onReject: (item: IngestPendingItem) => Promise<void>;
  onRetryParse: (item: IngestPendingItem) => Promise<void>;
  onRefresh?: () => void;
}

export function PendingDetail({
  item,
  previewText,
  previewLoading = false,
  onApprove,
  onReject,
  onRetryParse,
  onRefresh,
}: PendingDetailProps) {
  return (
    <PendingDetailContent
      key={`${item.id}:${item.mime_type}`}
      item={item}
      previewText={previewText}
      previewLoading={previewLoading}
      onApprove={onApprove}
      onReject={onReject}
      onRetryParse={onRetryParse}
      onRefresh={onRefresh}
    />
  );
}

function PendingDetailContent({
  item,
  previewText,
  previewLoading = false,
  onApprove,
  onReject,
  onRetryParse,
  onRefresh,
}: PendingDetailProps) {
  const [activeTab, setActiveTab] = useState<InspectorTab>("preview");
  const [state, setState] = useState<ItemState>({ status: "idle" });
  const [moveSelectorOpen, setMoveSelectorOpen] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  const busy = state.status !== "idle" && state.status !== "error";
  const tone = statusTone(item.status);
  const canChangeProject = PROJECT_CHANGE_ALLOWED_STATES.has(item.status);

  async function run(kind: "approve" | "reject" | "retry") {
    setState({ status: actionState(kind) });
    try {
      if (kind === "approve") await onApprove(item);
      else if (kind === "reject") await onReject(item);
      else await onRetryParse(item);
    } catch (err) {
      setState({ status: "error", error: err instanceof Error ? err.message : "Action failed" });
      return;
    }
  }

  async function handleDeleteConfirm() {
    setDeleteConfirmOpen(false);
    setState({ status: "deleting" });
    try {
      await deleteIngestPending(item.id);
      onRefresh?.();
    } catch (err) {
      setState({ status: "error", error: err instanceof Error ? err.message : "Delete failed" });
    }
  }

  async function handleProjectChange(newSlug: string) {
    if (!newSlug || newSlug === item.project_slug) {
      setMoveSelectorOpen(false);
      return;
    }
    setState({ status: "moving" });
    try {
      await patchIngestPending(
        item.id,
        { project_slug: newSlug },
        { ifMatch: item.updated_at }
      );
      setMoveSelectorOpen(false);
      setState({ status: "idle" });
      onRefresh?.();
    } catch (err) {
      setState({ status: "error", error: projectChangeErrorMessage(err) });
    }
  }

  return (
    <article className="flex h-full min-h-0 flex-col bg-pir-base">
      <header className="shrink-0 border-b border-pir bg-pir-surface-0">
        <div className="flex items-start gap-3 px-5 py-4">
          <MimeIcon item={item} size="large" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate font-display text-[19px] font-bold leading-tight tracking-[-0.01em] text-pir-text-primary">
                {fileLabel(item)}
              </h2>
              <span
                className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-[0.08em] ${tone.badge}`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
                {formatStatus(item.status)}
              </span>
              <SagaTimelineCompact item={item} activeState={state.status} />
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => canChangeProject && setMoveSelectorOpen(true)}
                disabled={!canChangeProject || state.status === "moving"}
                title={
                  canChangeProject
                    ? "Click per cambiare progetto"
                    : `Project change non consentito in stato ${item.status}`
                }
                className="inline-flex h-7 items-center gap-1.5 rounded-sm border border-pir-accent/40 bg-pir-accent/10 px-2 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-pir-accent transition-colors hover:border-pir-accent hover:bg-pir-accent/15 focus:border-pir-accent focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                aria-label={`Project: ${item.project_slug}${canChangeProject ? " (click to change)" : ""}`}
              >
                <FolderGlyph />
                <span>{item.project_slug}</span>
                {canChangeProject && (
                  <ChevronDownGlyph className={state.status === "moving" ? "animate-pulse" : ""} />
                )}
              </button>
              {canChangeProject && (() => {
                const llm = item.classification?.llm_metadata;
                const llmSlug = llm?.project_slug;
                const llmConf = llmConfidence(llm);
                if (!llmSlug || llmSlug === item.project_slug) return null;
                const confSuffix = llmConf != null ? ` (conf ${llmConf.toFixed(2)})` : "";
                const tooltip = `LLM suggerisce: ${llmSlug}${confSuffix}`;
                return (
                  <button
                    type="button"
                    onClick={() => void handleProjectChange(llmSlug)}
                    disabled={state.status === "moving"}
                    title={tooltip}
                    className="inline-flex h-7 items-center gap-1.5 rounded-sm border border-pir-success/50 bg-pir-success/10 px-2 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-pir-success transition-colors hover:border-pir-success hover:bg-pir-success/20 focus:border-pir-success focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                    aria-label={`Sposta in ${llmSlug} (suggerito da LLM)`}
                  >
                    <span>→ {llmSlug}</span>
                    {llmConf != null && (
                      <span className="text-pir-success/80">{(llmConf * 100).toFixed(0)}%</span>
                    )}
                  </button>
                );
              })()}
              <div className="flex flex-wrap items-center gap-2 font-mono text-caption text-pir-text-tertiary">
                <span>{item.mime_type ?? "unknown"}</span>
                <span aria-hidden="true">·</span>
                <span>{formatBytes(item.file_size_bytes)}</span>
                <span aria-hidden="true">·</span>
                <span>{item.parser_used ?? "parser -"}</span>
              </div>
            </div>
          </div>
        </div>

        <nav className="flex overflow-x-auto border-t border-pir bg-pir-base px-2" aria-label="Inspector tabs">
          {TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`relative px-4 py-2.5 font-sans text-label capitalize transition-colors focus:outline-none ${
                activeTab === tab
                  ? "text-pir-text-primary"
                  : "text-pir-text-tertiary hover:text-pir-text-primary"
              }`}
            >
              {tabLabel(tab)}
              {activeTab === tab && (
                <span className="absolute bottom-0 left-3 right-3 h-0.5 bg-pir-success" aria-hidden="true" />
              )}
            </button>
          ))}
        </nav>
      </header>

      {item.status === "parse_error" && (
        <ParseErrorBanner
          message={item.error_message}
          busy={busy}
          onRetry={() => void run("retry")}
          onQuarantine={() => void run("reject")}
        />
      )}
      {state.status === "error" && (
        <div className="border-b border-pir-error bg-pir-error/10 px-5 py-2 font-mono text-caption text-pir-error" role="alert">
          {state.error}
        </div>
      )}

      <section className="min-h-0 flex-1 overflow-auto" key={`${item.id}:${item.mime_type}:${activeTab}`}>
        {activeTab === "preview" && (
          <MimePolymorphicPreview item={item} text={previewText} loading={previewLoading} />
        )}
        {activeTab === "extract" && <ExtractPane item={item} text={previewText} />}
        {activeTab === "proposal" && <ProposalPane item={item} />}
        {activeTab === "saga" && <SagaPane item={item} activeState={state.status} />}
        {activeTab === "raw" && <RawPane item={item} />}
      </section>

      <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-pir bg-pir-surface-1 px-5 py-3">
        <p className="min-w-0 truncate font-mono text-caption text-pir-text-tertiary">
          {item.target_folder && item.target_filename
            ? `${item.target_folder}/${item.target_filename}`
            : basename(item.file_path)}
        </p>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => setDeleteConfirmOpen(true)}
            title="Elimina row + file fisici (irreversibile)"
            className="h-8 rounded-sm border border-pir-error/40 bg-transparent px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-error transition-colors hover:border-pir-error hover:bg-pir-error/10 disabled:cursor-wait disabled:opacity-50 focus:outline-none"
          >
            {state.status === "deleting" ? "Elimino..." : "Elimina"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void run("reject")}
            className="h-8 rounded-sm border border-pir bg-transparent px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-error hover:text-pir-error disabled:cursor-wait disabled:opacity-50 focus:border-pir-accent focus:outline-none"
          >
            Reject
          </button>
          <button
            type="button"
            disabled={busy || item.status !== "awaiting_triage"}
            onClick={() => void run("approve")}
            className="h-8 rounded-sm border border-pir-accent bg-pir-accent px-3 font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-base transition-opacity hover:opacity-90 disabled:cursor-wait disabled:opacity-50 focus:border-pir-accent focus:outline-none"
          >
            {state.status === "approving" ? "Approvazione..." : "Approve"}
          </button>
        </div>
      </footer>

      {moveSelectorOpen && (
        <ProjectSelectorModal
          currentSlug={item.project_slug}
          onSubmit={(slug) => void handleProjectChange(slug)}
          onClose={() => setMoveSelectorOpen(false)}
          filter={(p) => p.on_server === true && p.path !== null}
        />
      )}
      {deleteConfirmOpen && (
        <DeleteConfirmDialog
          filename={fileLabel(item)}
          onCancel={() => setDeleteConfirmOpen(false)}
          onConfirm={() => void handleDeleteConfirm()}
        />
      )}
    </article>
  );
}

function DeleteConfirmDialog({
  filename,
  onCancel,
  onConfirm,
}: {
  filename: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-ingest-title"
      aria-describedby="delete-ingest-description"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={onCancel}
    >
      <div
        onClick={(event) => event.stopPropagation()}
        className="w-full max-w-md rounded-sm border border-pir bg-pir-surface-0 shadow-2xl"
      >
        <header className="border-b border-pir px-5 py-3">
          <h2
            id="delete-ingest-title"
            className="font-display text-[16px] font-bold text-pir-text-primary"
          >
            Eliminare file
          </h2>
        </header>
        <div className="space-y-3 px-5 py-4 font-mono text-caption text-pir-text-secondary">
          <p className="break-all text-pir-text-primary">{filename}</p>
          <p id="delete-ingest-description">
            La row e i file fisici associati saranno rimossi. Operazione irreversibile.
          </p>
        </div>
        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-pir px-5 py-3">
          <button
            type="button"
            onClick={onCancel}
            className="h-8 rounded-sm border border-pir bg-transparent px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
          >
            Annulla
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="h-8 rounded-sm border border-pir-error bg-pir-error px-3 font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-base transition-opacity hover:opacity-90 focus:border-pir-error focus:outline-none"
          >
            Elimina
          </button>
        </footer>
      </div>
    </div>
  );
}

function projectChangeErrorMessage(err: unknown): string {
  if (err instanceof APIError && err.status === 409) {
    const code = apiErrorCode(err.detail);
    if (code === "target_sha_collision") {
      const existingId = apiErrorString(err.detail, "existing_ingest_id");
      return existingId
        ? `Contenuto gia presente nel progetto target. Row esistente: ${existingId}.`
        : "Contenuto gia presente nel progetto target. Apri la row esistente o elimina il duplicato.";
    }
    if (code === "target_filename_collision") {
      return "Nome file gia presente nel progetto target.";
    }
    if (code === "source_file_missing") {
      return "File sorgente non trovato su disco. Ricarica e verifica la row.";
    }
    return "Modifica concorrente — ricarica e riprova";
  }
  if (err instanceof Error) return err.message;
  return "Errore sposta progetto";
}

function apiErrorCode(detail: unknown): string | null {
  if (!detail || typeof detail !== "object" || !("error" in detail)) return null;
  const error = (detail as { error?: unknown }).error;
  return typeof error === "string" ? error : null;
}

function apiErrorString(detail: unknown, key: string): string | null {
  if (!detail || typeof detail !== "object" || !(key in detail)) return null;
  const value = (detail as Record<string, unknown>)[key];
  return typeof value === "string" ? value : null;
}

function llmConfidence(
  llm: NonNullable<IngestPendingItem["classification"]>["llm_metadata"] | undefined
): number | null {
  if (!llm) return null;
  if (typeof llm.composite_confidence === "number") return llm.composite_confidence;
  if (typeof llm.llm_confidence === "number") return llm.llm_confidence;
  if (typeof llm.confidence === "number") return llm.confidence;
  return null;
}

function llmAutoDecision(
  llm: NonNullable<IngestPendingItem["classification"]>["llm_metadata"]
): string {
  if (!llm) return "-";
  if (llm.auto_rejected) return "auto-rejected";
  return llm.auto_approved ? "yes" : "no";
}

function sourcePriorLabel(
  llm: NonNullable<IngestPendingItem["classification"]>["llm_metadata"] | undefined
): string {
  if (!llm?.source_project_slug) return "-";
  const prior = typeof llm.source_project_prior === "number"
    ? ` ${Math.round(llm.source_project_prior * 100)}%`
    : "";
  let result = "seen";
  if (llm.source_project_followed) result = "followed";
  else if (llm.source_project_overridden) result = "overridden";
  return `${llm.source_project_slug}${prior} · ${result}`;
}

function ingestV2Diagnostics(item: IngestPendingItem): Array<[string, string]> {
  const structure = asRecord(item.structure);
  const ingestV2 = asRecord(structure?.ingest_v2);
  if (!ingestV2) return [];

  const route = asRecord(ingestV2.route);
  const parserQuality = asRecord(ingestV2.parser_quality);
  const sourceContext = asRecord(ingestV2.source_context);
  const imageProbe = asRecord(ingestV2.image_probe);
  const rows: Array<[string, string]> = [];

  if (route) {
    rows.push(["workflow", valueLabel(route.workflow)]);
    rows.push(["tier", valueLabel(route.tier)]);
    rows.push(["mode", valueLabel(route.mode)]);
    rows.push(["route confidence", percentLabel(route.confidence)]);
    rows.push(["route reason", valueLabel(route.reason)]);
  }
  if (parserQuality) {
    rows.push(["parser quality", percentLabel(parserQuality.score)]);
  }
  if (sourceContext) {
    rows.push(["source project", valueLabel(sourceContext.project_slug)]);
    rows.push(["source prior", percentLabel(sourceContext.prior)]);
    rows.push(["source reason", valueLabel(sourceContext.reason)]);
  }
  if (imageProbe) {
    rows.push(["image kind", valueLabel(imageProbe.image_kind)]);
    rows.push(["doc likelihood", percentLabel(imageProbe.document_likelihood)]);
    rows.push(["screenshot likelihood", percentLabel(imageProbe.screenshot_likelihood)]);
    rows.push(["photo likelihood", percentLabel(imageProbe.photo_likelihood)]);
    rows.push(["text likelihood", percentLabel(imageProbe.text_likelihood)]);
    rows.push(["image signals", arrayLabel(imageProbe.signals)]);
  }

  return rows.filter(([, value]) => value !== "-");
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function valueLabel(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function percentLabel(value: unknown): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${Math.round(value * 100)}%`;
}

function arrayLabel(value: unknown): string {
  if (!Array.isArray(value) || value.length === 0) return "-";
  return value.map(String).join(", ");
}

function FolderGlyph() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 4.5a1 1 0 0 1 1-1h3.5l1.5 1.5H13a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4.5Z" />
    </svg>
  );
}

function ChevronDownGlyph({ className }: { className?: string }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      <path d="m4 6 4 4 4-4" />
    </svg>
  );
}

function MimePolymorphicPreview({
  item,
  text,
  loading,
}: {
  item: IngestPendingItem;
  text: string;
  loading: boolean;
}) {
  const kind = previewKind(item);
  if (kind === "pdf") return <PdfPreview item={item} />;
  if (kind === "xlsx") return <XlsxPreview item={item} />;
  if (kind === "image") return <ImagePreview item={item} />;
  return <MarkdownPreview item={item} text={text} loading={loading} />;
}

function ExtractPane({ item, text }: { item: IngestPendingItem; text: string }) {
  const source = text || item.extracted_text || "";
  const chunks = source.split(/\n{2,}/).filter(Boolean).slice(0, 5);
  return (
    <div className="space-y-4 p-5">
      <SectionLabel>Riassunto</SectionLabel>
      <div className="rounded-sm border border-pir bg-pir-surface-0 p-4 font-sans text-body leading-6 text-pir-text-secondary">
        {item.classification?.reason || source.slice(0, 420) || "Nessun estratto disponibile."}
      </div>
      <SectionLabel>Chunks · per embedding</SectionLabel>
      <div className="space-y-2">
        {(chunks.length ? chunks : ["empty chunk"]).map((chunk, index) => (
          <div key={index} className="flex items-center gap-3 rounded-sm border border-pir bg-pir-surface-0 px-3 py-2">
            <span className="w-8 font-mono text-caption font-bold text-pir-text-tertiary">
              #{index + 1}
            </span>
            <span className="min-w-0 flex-1 truncate font-sans text-body text-pir-text-secondary">
              {chunk}
            </span>
            <span className="rounded-sm bg-pir-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-pir-text-muted">
              {chunk.length}c
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ProposalPane({ item }: { item: IngestPendingItem }) {
  const classification = item.classification;
  const llm = classification?.llm_metadata;
  const llmConf = llmConfidence(llm);
  const tags = classification?.tags ?? [];
  const isInserted = item.status === "inserted" || item.status === "done";
  const pipelineRows = ingestV2Diagnostics(item);

  // UX-3: tre sezioni che riflettono il flusso pipeline.
  // 1. Classification deterministica (sempre presente, fallback senza LLM)
  // 2. LLM E5 enrichment (se l'LLM ha proposto qualcosa, sotto threshold o no)
  // 3. KG impact preview (cosa il file produrra' nel grafo post-saga)

  const deterministicRows: Array<[string, string]> = [
    ["node_type", classification?.type ?? "-"],
    ["title", classification?.title ?? fileLabel(item)],
    ["target_folder", item.target_folder ?? classification?.target_folder ?? "-"],
    ["target_filename", item.target_filename ?? classification?.target_filename ?? "-"],
    [
      "confidence",
      typeof classification?.confidence === "number"
        ? `${Math.round(classification.confidence * 100)}%`
        : "-",
    ],
    ["reason", classification?.reason ?? "-"],
  ];

  return (
    <div className="space-y-5 p-5">
      <ProposalSection label="Classification deterministica" tone="neutral">
        <KeyValueGrid rows={deterministicRows} />
        {tags.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {tags.map((tag) => (
              <span
                key={tag}
                className="rounded-sm border border-pir bg-pir-surface-1 px-1.5 py-0.5 font-mono text-[10px] text-pir-text-secondary"
              >
                {tag}
              </span>
            ))}
          </div>
        )}
      </ProposalSection>

      {pipelineRows.length > 0 && (
        <ProposalSection label="Pipeline diagnostics" tone="neutral">
          <KeyValueGrid rows={pipelineRows} />
        </ProposalSection>
      )}

      <ProposalSection label="LLM enrichment (E5)" tone={llm ? "accent" : "muted"}>
        {llm ? (
          <>
            <KeyValueGrid
              rows={[
                ["model", llm.model ?? "-"],
                ["provider", llm.provider ?? "-"],
                ["status", llm.status ?? "ok"],
                ["reason", llm.reason ?? "-"],
                ["suggested project", llm.project_slug ?? "-"],
                ["valid project", llm.valid_slug ?? "-"],
                ["source prior", sourcePriorLabel(llm)],
                ["document_type", llm.document_type ?? "-"],
                [
                  "confidence",
                  llmConf != null ? `${Math.round(llmConf * 100)}%` : "-",
                ],
                ["decision", llmAutoDecision(llm)],
                [
                  "block",
                  llm.auto_approve_blocked_reason ?? llm.auto_reject_reason ?? "-",
                ],
                ["existing row", llm.existing_ingest_id ?? "-"],
              ]}
            />
            {llm.title && (
              <div className="mt-3">
                <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-pir-text-tertiary">
                  LLM title
                </p>
                <p className="mt-1 font-sans text-body text-pir-text-primary">{llm.title}</p>
              </div>
            )}
            {llm.tags && llm.tags.length > 0 && (
              <div className="mt-3">
                <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-pir-text-tertiary">
                  LLM tags
                </p>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {llm.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-sm border border-pir-accent/40 bg-pir-accent/10 px-1.5 py-0.5 font-mono text-[10px] text-pir-accent"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {llm.reasoning && (
              <div className="mt-3">
                <p className="font-mono text-[10px] uppercase tracking-[0.08em] text-pir-text-tertiary">
                  Reasoning
                </p>
                <p className="mt-1 whitespace-pre-wrap font-mono text-caption leading-5 text-pir-text-secondary">
                  {llm.reasoning}
                </p>
              </div>
            )}
          </>
        ) : (
          <p className="font-mono text-caption text-pir-text-tertiary">
            LLM classifier non ha prodotto un&apos;analisi semantica per questo file
            (possibili cause: feature flag off, errore API, file non-markdown).
          </p>
        )}
      </ProposalSection>

      <ProposalSection label="KG impact preview" tone={isInserted ? "success" : "muted"}>
        {isInserted ? (
          <KeyValueGrid
            rows={[
              ["kg_node", computeKgNodeId(classification?.type, item.project_slug, item.target_folder, item.target_filename)],
              ["edge", `project:artifact:${item.project_slug} --contains→ ${classification?.type ?? "file"}:artifact:...`],
              [
                "enrichment",
                "edges suggested via local tier-fast (mentions/refers_to/similar_to/cites) — vedi tab Saga",
              ],
            ]}
          />
        ) : (
          <p className="font-mono text-caption text-pir-text-tertiary">
            Il KG impact preview e&apos; disponibile dopo che il file e&apos; stato
            approvato e la saga ha indicizzato il nodo (status: inserted/done).
          </p>
        )}
      </ProposalSection>
    </div>
  );
}

function ProposalSection({
  label,
  tone,
  children,
}: {
  label: string;
  tone: "neutral" | "accent" | "success" | "muted";
  children: ReactNode;
}) {
  const accentMap: Record<typeof tone, string> = {
    accent: "border-l-pir-accent",
    success: "border-l-pir-success",
    muted: "border-l-pir-surface-3",
    neutral: "border-l-pir-text-tertiary",
  };
  const accent = accentMap[tone];
  return (
    <section className={`rounded-sm border border-pir bg-pir-surface-0 border-l-2 ${accent}`}>
      <header className="border-b border-pir px-3 py-2">
        <SectionLabel>{label}</SectionLabel>
      </header>
      <div className="p-3">{children}</div>
    </section>
  );
}

function KeyValueGrid({ rows }: { rows: Array<[string, string]> }) {
  return (
    <div className="rounded-sm border border-pir bg-pir-surface-1">
      {rows.map(([label, value]) => (
        <div
          key={label}
          className="grid grid-cols-[140px_minmax(0,1fr)] gap-3 border-b border-pir px-3 py-2 last:border-b-0"
        >
          <dt className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
            {label}
          </dt>
          <dd className="truncate font-mono text-caption text-pir-text-secondary">{value}</dd>
        </div>
      ))}
    </div>
  );
}

function computeKgNodeId(
  documentType: string | undefined,
  projectSlug: string,
  targetFolder: string | null,
  targetFilename: string | null
): string {
  if (!targetFolder || !targetFilename) return "-";
  const type = documentType || "file";
  return `${type}:artifact:${projectSlug}/${targetFolder}/${targetFilename}`;
}

function SagaTimelineCompact({
  item,
  activeState,
}: {
  item: IngestPendingItem;
  activeState: ItemState["status"];
}) {
  const steps = sagaSteps(item, activeState);
  const summary = steps.map(([label, status]) => `${label}: ${status}`).join(" · ");
  return (
    <div
      className="ml-auto flex items-center gap-1"
      role="list"
      aria-label={`Pipeline saga: ${summary}`}
      title={summary}
    >
      {steps.map(([label, status], idx) => (
        <span key={label} role="listitem" className="flex items-center gap-1">
          <span
            aria-label={`${label}: ${status}`}
            className={`h-1.5 w-1.5 rounded-full ${sagaDotClass(status)}`}
          />
          {idx < steps.length - 1 && (
            <span aria-hidden="true" className="h-px w-3 bg-pir-surface-3" />
          )}
        </span>
      ))}
    </div>
  );
}

function SagaPane({ item, activeState }: { item: IngestPendingItem; activeState: ItemState["status"] }) {
  const steps = sagaSteps(item, activeState);
  return (
    <div className="space-y-4 p-5">
      <SectionLabel>Saga · ingestion pipeline</SectionLabel>
      <div className="rounded-sm border border-pir bg-pir-surface-0">
        {steps.map(([label, status]) => (
          <div key={label} className="flex items-center gap-3 border-b border-pir px-3 py-2 last:border-b-0">
            <span className={`h-2 w-2 rounded-full ${sagaDotClass(status)}`} />
            <span className="flex-1 font-mono text-caption text-pir-text-secondary">{label}</span>
            <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-pir-text-tertiary">
              {status}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RawPane({ item }: { item: IngestPendingItem }) {
  return (
    <pre className="m-5 overflow-auto rounded-sm border border-pir bg-pir-surface-0 p-4 font-mono text-caption leading-5 text-pir-text-secondary">
      {JSON.stringify(item, null, 2)}
    </pre>
  );
}

type BusyState = Extract<ItemState["status"], "approving" | "rejecting" | "retrying">;

function actionState(kind: "approve" | "reject" | "retry"): BusyState {
  if (kind === "approve") return "approving";
  if (kind === "reject") return "rejecting";
  return "retrying";
}

function tabLabel(tab: InspectorTab): string {
  if (tab === "preview") return "Anteprima";
  if (tab === "extract") return "Estratto";
  if (tab === "proposal") return "Proposta KG";
  return tab;
}

type SagaStatus = "done" | "running" | "pending" | "error";
type SagaStep = [string, SagaStatus];

function sagaSteps(
  item: IngestPendingItem,
  activeState: ItemState["status"]
): SagaStep[] {
  return [
    ["parse_file", item.status === "parse_error" ? "error" : "done"],
    [
      "classify",
      item.status === "queued" ||
      item.status === "parser_waiting" ||
      item.status === "parsing"
        ? "pending"
        : "done",
    ],
    ["triage", item.status === "awaiting_triage" ? "running" : "done"],
    ["insert_file", insertFileStatus(item, activeState)],
    ["index_kg", indexKgStatus(item)],
  ];
}

function insertFileStatus(
  item: IngestPendingItem,
  activeState: ItemState["status"]
): SagaStatus {
  if (item.status === "inserted" || item.status === "done") return "done";
  if (activeState === "approving" || item.status === "approved") return "running";
  return "pending";
}

function indexKgStatus(item: IngestPendingItem): SagaStatus {
  if (item.status === "done") return "done";
  if (item.status === "inserted") return "running";
  return "pending";
}

function sagaDotClass(status: SagaStatus): string {
  if (status === "done") return "bg-pir-success";
  if (status === "running") return "animate-pulse bg-pir-accent";
  if (status === "error") return "bg-pir-error";
  return "bg-pir-surface-3";
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="font-mono text-caption font-semibold uppercase tracking-[0.14em] text-pir-text-tertiary">
      {children}
    </p>
  );
}
