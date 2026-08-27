"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getIngestPreviewMd,
  listIngestPending,
  listIngestSkipped,
} from "@/lib/api";
import type { IngestPendingItem, IngestSkipEntry } from "@/lib/types";
import { AutoApprovedCounter } from "@/components/ingest/AutoApprovedCounter";
import { PendingDetail } from "@/components/ingest/PendingDetail";
import {
  ACTIVE_INGEST_STATUSES,
  PendingList,
} from "@/components/ingest/PendingList";
import { EmptyState } from "@/components/ingest/states/EmptyState";
import { LoadingState } from "@/components/ingest/states/LoadingState";
import { previewKind } from "@/components/ingest/format";

const ACTIVE_STATUS_FETCH_LIMIT = 80;

type FetchState =
  | { status: "loading"; items: IngestPendingItem[] }
  | { status: "ready"; items: IngestPendingItem[] }
  | { status: "error"; items: IngestPendingItem[]; error: string };

function isOnIngestRoute(): boolean {
  return typeof window === "undefined" || window.location.pathname.includes("/inbox/triage/files");
}

function usesExtractedTextPreview(item: IngestPendingItem): boolean {
  const mime = item.mime_type ?? "";
  return item.parser_used === "tier_transcribe" || mime.startsWith("audio/") || mime.startsWith("video/");
}

function sortNewestFirst(items: IngestPendingItem[]) {
  return [...items].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
}

export default function IngestFilesTriagePage() {
  const [fetchState, setFetchState] = useState<FetchState>({ status: "loading", items: [] });
  const [skippedItems, setSkippedItems] = useState<IngestSkipEntry[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [previewText, setPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const refreshIdRef = useRef(0);

  const refresh = useCallback(async ({ showLoading = false }: { showLoading?: boolean } = {}) => {
    const refreshId = ++refreshIdRef.current;
    if (showLoading) setFetchState((current) => ({ status: "loading", items: current.items }));
    try {
      const [buckets, skips] = await Promise.all([
        Promise.all(ACTIVE_INGEST_STATUSES.map((status) => listIngestPending({ status, limit: ACTIVE_STATUS_FETCH_LIMIT }))),
        listIngestSkipped({ limit: 100 }).catch(() => [] as IngestSkipEntry[]),
      ]);
      if (refreshId !== refreshIdRef.current) return;
      const items = sortNewestFirst(buckets.flat());
      const ids = new Set(items.map((item) => item.id));
      setFetchState({ status: "ready", items });
      setSkippedItems(skips);
      setSelectedIds((current) => new Set([...current].filter((id) => ids.has(id))));
      setSelectedId((current) => current && ids.has(current) ? current : items[0]?.id ?? null);
    } catch (err) {
      if (refreshId !== refreshIdRef.current) return;
      setFetchState((current) => ({
        status: "error",
        items: current.items,
        error: err instanceof Error ? err.message : "Failed to load ingest queue",
      }));
    }
  }, []);

  useEffect(() => {
    void refresh({ showLoading: true });
  }, [refresh]);

  useEffect(() => {
    const handler = () => {
      if (isOnIngestRoute()) void refresh();
    };
    window.addEventListener("marvisx:ingest_changed", handler);
    window.addEventListener("focus", handler);
    return () => {
      window.removeEventListener("marvisx:ingest_changed", handler);
      window.removeEventListener("focus", handler);
    };
  }, [refresh]);

  const items = fetchState.items;
  const selected = useMemo(
    () => items.find((item) => item.id === selectedId) ?? items[0] ?? null,
    [items, selectedId],
  );

  useEffect(() => {
    if (!selected || previewKind(selected) !== "markdown" || usesExtractedTextPreview(selected)) {
      setPreviewText(selected?.extracted_text ?? "");
      setPreviewLoading(false);
      return;
    }
    const controller = new AbortController();
    setPreviewLoading(true);
    getIngestPreviewMd(selected.id, { signal: controller.signal })
      .then((text) => {
        if (!controller.signal.aborted) setPreviewText(text);
      })
      .catch(() => {
        if (!controller.signal.aborted) setPreviewText(selected.extracted_text ?? "");
      })
      .finally(() => {
        if (!controller.signal.aborted) setPreviewLoading(false);
      });
    return () => controller.abort();
  }, [selected]);

  const toggleSelection = useCallback((id: string, nextSelected: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (nextSelected) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const toggleGroupSelection = useCallback((ids: string[], nextSelected: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (nextSelected) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }, []);

  if (fetchState.status === "loading" && items.length === 0) return <LoadingState />;

  return (
    <main className="flex h-full min-h-0 flex-col bg-pir-base text-pir-text-primary">
      <header className="flex shrink-0 items-center gap-3 border-b border-pir bg-pir-surface-0 px-4 py-3">
        <div className="min-w-0">
          <h1 className="font-display text-heading text-pir-text-primary">File ingest triage</h1>
          <p className="mt-1 font-mono text-caption text-pir-text-tertiary">
            {items.length} active rows · inspection view
          </p>
        </div>
        <div className="ml-auto">
          <AutoApprovedCounter />
        </div>
        <button type="button" onClick={() => void refresh({ showLoading: true })} className="rounded border border-pir px-2.5 py-1.5 font-mono text-caption text-pir-text-secondary hover:border-pir-accent">
          Refresh
        </button>
      </header>

      {fetchState.status === "error" && (
        <div className="shrink-0 border-b border-pir-error bg-pir-error/10 px-4 py-2 font-mono text-caption text-pir-error" role="alert">
          {fetchState.error}
        </div>
      )}

      <section className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[380px_minmax(0,1fr)]">
        <PendingList
          items={items}
          skippedItems={skippedItems}
          selectedId={selected?.id ?? null}
          selectedIds={selectedIds}
          loading={fetchState.status === "loading"}
          onSelect={setSelectedId}
          onToggleSelection={toggleSelection}
          onToggleGroupSelection={toggleGroupSelection}
          onRefresh={() => void refresh({ showLoading: true })}
        />

        <div className="min-h-0 bg-pir-base">
          {selected ? (
            <PendingDetail
              item={selected}
              previewText={previewText}
              previewLoading={previewLoading}
            />
          ) : (
            <EmptyState />
          )}
        </div>
      </section>
    </main>
  );
}
