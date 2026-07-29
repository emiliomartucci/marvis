"use client";

import { useMemo, useState } from "react";
import type {
  IngestPendingItem,
  IngestPendingStatus,
  IngestSkipEntry,
  IngestSkipReason,
} from "@/lib/types";
import { fileLabel, formatBytes, formatStatus, statusTone } from "./format";
import { MimeIcon } from "./MimeIcon";

export type IngestQueueFilter = IngestPendingStatus | "all";

// UX-4 (Phase 1.5): active pipeline only. Terminal audit states are not part
// of this work queue; showing done/rejected rows here makes stale files look
// actionable.
type GroupConfig = {
  status: IngestPendingStatus;
  label: string;
  defaultOpen: boolean;
};

const PIPELINE_GROUPS: GroupConfig[] = [
  { status: "queued", label: "Queued", defaultOpen: true },
  { status: "parser_waiting", label: "Parser waiting", defaultOpen: true },
  { status: "parsing", label: "Parsing", defaultOpen: true },
  { status: "classified", label: "Classified", defaultOpen: true },
  { status: "awaiting_triage", label: "Awaiting triage", defaultOpen: true },
  { status: "parse_error", label: "Parse error", defaultOpen: true },
  { status: "approved", label: "Approved", defaultOpen: false },
  { status: "inserted", label: "Inserted", defaultOpen: false },
];

export const ACTIVE_INGEST_STATUSES = PIPELINE_GROUPS.map(
  (group) => group.status
);

interface PendingListProps {
  items: IngestPendingItem[];
  // UX-6: skip-audit entries (dedup / mime / invalid). Rendered in a
  // separate "Ignorati" group below the pipeline groups, default collapsed.
  skippedItems?: IngestSkipEntry[];
  selectedId: string | null;
  selectedIds?: ReadonlySet<string>;
  loading?: boolean;
  onSelect: (id: string) => void;
  onToggleSelection?: (id: string, selected: boolean) => void;
  onToggleGroupSelection?: (ids: string[], selected: boolean) => void;
  onRefresh: () => void;
}

const SKIP_REASON_LABEL: Record<IngestSkipReason, string> = {
  dedup_sha256: "gia presente",
  invalid_path: "path non valido",
  mime_not_allowed: "mime non supportato",
  parse_error_pre_dispatch: "parser fallito",
};

export function PendingList({
  items,
  skippedItems = [],
  selectedId,
  selectedIds = new Set<string>(),
  loading = false,
  onSelect,
  onToggleSelection,
  onToggleGroupSelection,
  onRefresh,
}: PendingListProps) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {};
    for (const group of PIPELINE_GROUPS) {
      init[group.status] = !group.defaultOpen;
    }
    init["__skipped__"] = true; // UX-6: collapsed by default
    return init;
  });

  const grouped = useMemo(() => {
    const buckets: Record<string, IngestPendingItem[]> = {};
    for (const group of PIPELINE_GROUPS) buckets[group.status] = [];
    for (const item of items) {
      const bucket = buckets[item.status];
      if (bucket) bucket.push(item);
    }
    return buckets;
  }, [items]);

  function toggle(status: IngestPendingStatus) {
    setCollapsed((prev) => ({ ...prev, [status]: !prev[status] }));
  }

  return (
    <section className="flex h-full min-h-0 flex-col border-r border-pir bg-pir-surface-0">
      <header className="shrink-0 border-b border-pir px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="font-display text-[18px] font-bold leading-tight tracking-[-0.01em] text-pir-text-primary">
              /inbox/files
            </h2>
            <p className="mt-1 font-mono text-caption text-pir-text-tertiary">
              upload · api/zip/folder · {items.length} active
            </p>
          </div>
          <button
            type="button"
            onClick={onRefresh}
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-sm border border-pir bg-transparent font-mono text-caption text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
            aria-label="Aggiorna coda ingest"
          >
            R
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading && items.length === 0 ? (
          <QueueSkeleton />
        ) : (
          <>
            {PIPELINE_GROUPS.map((group) => {
              const bucket = grouped[group.status] ?? [];
              const isCollapsed = collapsed[group.status];
              return (
                <PipelineGroup
                  key={group.status}
                  group={group}
                  items={bucket}
                  collapsed={isCollapsed}
                  onToggle={() => toggle(group.status)}
                  selectedId={selectedId}
                  selectedIds={selectedIds}
                  onSelect={onSelect}
                  onToggleSelection={onToggleSelection}
                  onToggleGroupSelection={onToggleGroupSelection}
                />
              );
            })}
            <IgnoredGroup
              items={skippedItems}
              collapsed={collapsed["__skipped__"] ?? true}
              onToggle={() =>
                setCollapsed((prev) => ({
                  ...prev,
                  __skipped__: !(prev["__skipped__"] ?? true),
                }))
              }
            />
          </>
        )}
      </div>
    </section>
  );
}

function IgnoredGroup({
  items,
  collapsed,
  onToggle,
}: {
  items: IngestSkipEntry[];
  collapsed: boolean;
  onToggle: () => void;
}) {
  const expanded = !collapsed && items.length > 0;
  return (
    <div className="border-b border-pir last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        disabled={items.length === 0}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 bg-pir-surface-0 px-4 py-2 text-left transition-colors hover:bg-pir-surface-1 focus:bg-pir-surface-1 focus:outline-none disabled:opacity-60"
      >
        <span
          aria-hidden="true"
          className={`inline-block transition-transform ${expanded ? "rotate-90" : ""}`}
        >
          ▸
        </span>
        <span className="h-2 w-2 rounded-full bg-amber-400" />
        <span className="flex-1 font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-text-secondary">
          Ignorati
        </span>
        <span
          className={`min-w-[1.5rem] rounded-sm px-1.5 py-0.5 text-center font-mono text-[10px] font-semibold ${
            items.length === 0
              ? "bg-transparent text-pir-text-muted"
              : "bg-pir-surface-2 text-pir-text-primary"
          }`}
        >
          {items.length}
        </span>
      </button>
      {expanded && (
        <ul className="divide-y divide-pir">
          {items.map((entry) => (
            <li key={entry.id}>
              <SkipEntryRow entry={entry} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SkipEntryRow({ entry }: { entry: IngestSkipEntry }) {
  const filename = entry.file_path_attempted.split("/").pop() ?? entry.file_path_attempted;
  const reasonLabel = SKIP_REASON_LABEL[entry.reason];
  return (
    <div className="border-l-2 border-transparent px-4 py-3">
      <div className="truncate font-sans text-body font-semibold text-pir-text-primary">
        {filename}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1 rounded-sm border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-[0.08em] text-amber-400">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
          {reasonLabel}
        </span>
        <span className="rounded-sm border border-pir bg-pir-surface-2 px-1.5 py-0.5 font-mono text-[9px] text-pir-text-tertiary">
          {entry.project_slug}
        </span>
      </div>
      {entry.error_message && (
        <p className="mt-2 line-clamp-2 font-mono text-[10px] leading-4 text-pir-text-muted">
          {entry.error_message}
        </p>
      )}
    </div>
  );
}

function PipelineGroup({
  group,
  items,
  collapsed,
  onToggle,
  selectedId,
  selectedIds,
  onSelect,
  onToggleSelection,
  onToggleGroupSelection,
}: {
  group: GroupConfig;
  items: IngestPendingItem[];
  collapsed: boolean;
  onToggle: () => void;
  selectedId: string | null;
  selectedIds: ReadonlySet<string>;
  onSelect: (id: string) => void;
  onToggleSelection?: (id: string, selected: boolean) => void;
  onToggleGroupSelection?: (ids: string[], selected: boolean) => void;
}) {
  const tone = statusTone(group.status);
  const expanded = !collapsed && items.length > 0;
  const selectedCount = items.filter((item) => selectedIds.has(item.id)).length;
  const allSelected = items.length > 0 && selectedCount === items.length;
  const partiallySelected = selectedCount > 0 && !allSelected;
  const selectionEnabled = Boolean(onToggleSelection);
  return (
    <div className="border-b border-pir last:border-b-0">
      <div className="flex items-center gap-2 bg-pir-surface-0 px-4 py-2 transition-colors hover:bg-pir-surface-1">
        {selectionEnabled && (
          <label
            className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-sm border text-pir-text-primary ${
              partiallySelected
                ? "border-pir-accent bg-pir-accent/20"
                : "border-pir-strong bg-pir-surface-1"
            } ${items.length === 0 ? "opacity-40" : ""}`}
            title={`Seleziona ${group.label}`}
          >
            <input
              type="checkbox"
              className="h-3.5 w-3.5 accent-pir-accent"
              checked={allSelected}
              aria-checked={partiallySelected ? "mixed" : allSelected}
              disabled={items.length === 0}
              aria-label={`Seleziona gruppo ${group.label}`}
              onChange={(event) =>
                onToggleGroupSelection?.(
                  items.map((item) => item.id),
                  event.currentTarget.checked
                )
              }
            />
          </label>
        )}
        <button
          type="button"
          onClick={onToggle}
          disabled={items.length === 0}
          aria-expanded={expanded}
          className="flex min-w-0 flex-1 items-center gap-2 text-left focus:outline-none disabled:opacity-60"
        >
          <span
            aria-hidden="true"
            className={`inline-block transition-transform ${expanded ? "rotate-90" : ""}`}
          >
            ▸
          </span>
          <span className={`h-2 w-2 rounded-full ${tone.dot}`} />
          <span className="flex-1 font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-text-secondary">
            {group.label}
          </span>
          {selectionEnabled && selectedCount > 0 && (
            <span className="rounded-sm border border-pir-accent/40 bg-pir-accent/10 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-pir-accent">
              {selectedCount} sel.
            </span>
          )}
          <span
            className={`min-w-[1.5rem] rounded-sm px-1.5 py-0.5 text-center font-mono text-[10px] font-semibold ${
              items.length === 0
                ? "bg-transparent text-pir-text-muted"
                : "bg-pir-surface-2 text-pir-text-primary"
            }`}
          >
            {items.length}
          </span>
        </button>
      </div>
      {expanded && (
        <ul className="divide-y divide-pir">
          {items.map((item) => (
            <li key={item.id}>
              <QueueRow
                item={item}
                selected={item.id === selectedId}
                bulkSelected={selectedIds.has(item.id)}
                onSelect={onSelect}
                onToggleSelection={onToggleSelection}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function QueueRow({
  item,
  selected,
  bulkSelected,
  onSelect,
  onToggleSelection,
}: {
  item: IngestPendingItem;
  selected: boolean;
  bulkSelected: boolean;
  onSelect: (id: string) => void;
  onToggleSelection?: (id: string, selected: boolean) => void;
}) {
  const tone = statusTone(item.status);
  const label = fileLabel(item);
  return (
    <div
      className={`flex w-full gap-2 border-l-2 px-4 py-3 transition-colors ${
        selected
          ? "border-pir-accent bg-pir-accent/15"
          : "border-transparent bg-transparent hover:bg-pir-surface-1"
      } ${bulkSelected ? "ring-1 ring-inset ring-pir-accent/40" : ""}`}
    >
      {onToggleSelection && (
        <label className="mt-2 flex h-5 w-5 shrink-0 items-center justify-center rounded-sm border border-pir-strong bg-pir-surface-1">
          <input
            type="checkbox"
            className="h-3.5 w-3.5 accent-pir-accent"
            checked={bulkSelected}
            aria-label={`Seleziona ${label}`}
            onChange={(event) => onToggleSelection(item.id, event.currentTarget.checked)}
          />
        </label>
      )}
      <button
        type="button"
        onClick={() => onSelect(item.id)}
        className="flex min-w-0 flex-1 gap-3 text-left focus:outline-none focus:ring-1 focus:ring-pir-accent"
      >
        <MimeIcon item={item} />
        <div className="min-w-0 flex-1">
          <div className="truncate font-sans text-body font-semibold text-pir-text-primary">
            {label}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span
              className={`inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-[0.08em] ${tone.badge}`}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
              {formatStatus(item.status)}
            </span>
            {typeof item.classification?.confidence === "number" && (
              <span className="rounded-sm border border-pir bg-pir-surface-2 px-1.5 py-0.5 font-mono text-[9px] text-pir-text-tertiary">
                {Math.round(item.classification.confidence * 100)}%
              </span>
            )}
          </div>
          <div className="mt-2 flex min-w-0 items-center gap-2 font-mono text-[10px] text-pir-text-muted">
            <span>{formatBytes(item.file_size_bytes)}</span>
            <span aria-hidden="true">·</span>
            <span className="truncate">{item.project_slug}</span>
          </div>
          {item.error_message && (
            <p className="mt-2 line-clamp-2 font-mono text-[10px] leading-4 text-pir-error">
              {item.error_message}
            </p>
          )}
        </div>
      </button>
    </div>
  );
}

function QueueSkeleton() {
  return (
    <div className="space-y-0 divide-y divide-pir" aria-label="Caricamento coda">
      {Array.from({ length: 8 }).map((_, index) => (
        <div key={index} className="flex gap-3 px-4 py-3">
          <div className="h-10 w-8 animate-pulse rounded-sm bg-pir-surface-2" />
          <div className="flex-1 space-y-2">
            <div className="h-3 w-4/5 animate-pulse rounded-sm bg-pir-surface-2" />
            <div className="h-3 w-2/5 animate-pulse rounded-sm bg-pir-surface-2" />
            <div className="h-2 w-3/5 animate-pulse rounded-sm bg-pir-surface-2" />
          </div>
        </div>
      ))}
    </div>
  );
}
