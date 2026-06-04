"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  approveIngestPending,
  deleteIngestPending,
  getPrograms,
  getIngestPreviewMd,
  listIngestPending,
  listIngestSkipped,
  rejectIngestPending,
  retryParseIngestPending,
} from "@/lib/api";
import type { IngestPendingItem, IngestSkipEntry, ProjectInfo } from "@/lib/types";
import { AutoApprovedCounter } from "@/components/ingest/AutoApprovedCounter";
import { FolderUpload } from "@/components/ingest/FolderUpload";
import { PendingDetail } from "@/components/ingest/PendingDetail";
import {
  ACTIVE_INGEST_STATUSES,
  PendingList,
} from "@/components/ingest/PendingList";
import {
  isVisibleUploadProject,
  resolveIngestUploadProject,
  uploadableProjects,
} from "@/components/ingest/uploadProjectSelection";
import { EmptyState } from "@/components/ingest/states/EmptyState";
import { LoadingState } from "@/components/ingest/states/LoadingState";
import { ConnectionErrorToast } from "@/components/ingest/states/ConnectionErrorToast";
import { previewKind } from "@/components/ingest/format";
import ProjectSelectorModal from "@/components/ProjectSelectorModal";

const UPLOAD_PROJECT_STORAGE_KEY = "ingestUploadProject";
const ACTIVE_STATUS_FETCH_LIMIT = 80;

type FetchState =
  | { status: "loading"; items: IngestPendingItem[] }
  | { status: "ready"; items: IngestPendingItem[] }
  | { status: "error"; items: IngestPendingItem[]; error: string };

type BulkAction = "reparse" | "delete" | "approve";
type BulkNotice =
  | { tone: "info" | "success" | "error"; message: string }
  | null;

const BULK_ACTION_LABEL: Record<BulkAction, string> = {
  reparse: "reparse",
  delete: "delete",
  approve: "approve",
};

function isOnIngestRoute(): boolean {
  if (typeof window === "undefined") return true;
  return window.location.pathname.includes("/inbox/triage/files");
}

function usesExtractedTextPreview(item: IngestPendingItem): boolean {
  const mime = item.mime_type ?? "";
  return (
    item.parser_used === "tier_transcribe" ||
    mime.startsWith("audio/") ||
    mime.startsWith("video/")
  );
}

function sortNewestFirst(items: IngestPendingItem[]): IngestPendingItem[] {
  return [...items].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)
  );
}

function canBulkReparse(item: IngestPendingItem): boolean {
  return item.status === "parse_error" || item.status === "awaiting_triage";
}

function canBulkApprove(item: IngestPendingItem): boolean {
  return (
    item.status === "awaiting_triage" &&
    Boolean(item.target_folder) &&
    Boolean(item.target_filename)
  );
}

function canRunBulkAction(action: BulkAction, item: IngestPendingItem): boolean {
  if (action === "reparse") return canBulkReparse(item);
  if (action === "approve") return canBulkApprove(item);
  return true;
}

function bulkNoticeClass(notice: BulkNotice): string {
  if (notice?.tone === "error") return "text-pir-error";
  if (notice?.tone === "success") return "text-pir-success";
  return "text-pir-text-tertiary";
}

export default function IngestFilesTriagePage() {
  const [fetchState, setFetchState] = useState<FetchState>({
    status: "loading",
    items: [],
  });
  // UX-6: skip-audit log feeds the "Ignorati" sidebar group. Fetched in parallel
  // with the pending queue and refreshed on every onRefresh tick.
  const [skippedItems, setSkippedItems] = useState<IngestSkipEntry[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [bulkRunning, setBulkRunning] = useState<BulkAction | null>(null);
  const [bulkNotice, setBulkNotice] = useState<BulkNotice>(null);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [previewText, setPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [wsConnected] = useState(true);
  const [uploadProjects, setUploadProjects] = useState<ProjectInfo[]>([]);
  const [uploadProjectsLoading, setUploadProjectsLoading] = useState(true);
  const [uploadProjectsError, setUploadProjectsError] = useState<string | null>(null);
  const [selectedProjectSlug, setSelectedProjectSlug] = useState<string>("");
  const [selectorOpen, setSelectorOpen] = useState(false);
  const refreshIdRef = useRef(0);

  // Resolve the upload target from the projects visible to the current user.
  // The localStorage value may come from a different account on the same
  // browser, so it is only accepted after the RBAC-filtered project list loads.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const controller = new AbortController();
    getPrograms({ signal: controller.signal })
      .then((programs) => {
        if (controller.signal.aborted) return;
        const visible = uploadableProjects(programs);
        const stored = window.localStorage.getItem(UPLOAD_PROJECT_STORAGE_KEY);
        const nextSlug = resolveIngestUploadProject(stored, visible);
        setUploadProjects(visible);
        setSelectedProjectSlug(nextSlug);
        setUploadProjectsError(null);
        if (nextSlug) window.localStorage.setItem(UPLOAD_PROJECT_STORAGE_KEY, nextSlug);
        else window.localStorage.removeItem(UPLOAD_PROJECT_STORAGE_KEY);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setUploadProjects([]);
        setSelectedProjectSlug("");
        setUploadProjectsError(
          err instanceof Error ? err.message : "Failed to load projects"
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setUploadProjectsLoading(false);
      });
    return () => controller.abort();
  }, []);

  const uploadProjectSlugs = useMemo(
    () => new Set(uploadProjects.map((project) => project.slug)),
    [uploadProjects]
  );

  // The upload target is deliberately persisted only after RBAC-filtered
  // projects have loaded; otherwise a previous user's cached slug can leak into
  // this user's upload flow.
  /* eslint-disable react-you-might-not-need-an-effect/no-event-handler */
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (uploadProjectsLoading) return;
    if (selectedProjectSlug) {
      window.localStorage.setItem(UPLOAD_PROJECT_STORAGE_KEY, selectedProjectSlug);
    } else {
      window.localStorage.removeItem(UPLOAD_PROJECT_STORAGE_KEY);
    }
  }, [selectedProjectSlug, uploadProjectsLoading]);
  /* eslint-enable react-you-might-not-need-an-effect/no-event-handler */

  // H-D7 — sync across tabs via storage event.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handler = (event: StorageEvent) => {
      if (event.key === UPLOAD_PROJECT_STORAGE_KEY) {
        setSelectedProjectSlug(
          resolveIngestUploadProject(event.newValue, uploadProjects)
        );
      }
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, [uploadProjects]);

  const items = fetchState.items;
  const selected = useMemo(
    () => items.find((item) => item.id === selectedId) ?? items[0] ?? null,
    [items, selectedId]
  );
  const selectedItems = useMemo(
    () => items.filter((item) => selectedIds.has(item.id)),
    [items, selectedIds]
  );
  const reparseCount = selectedItems.filter(canBulkReparse).length;
  const approveCount = selectedItems.filter(canBulkApprove).length;

  const refresh = useCallback(
    async ({ showLoading = false }: { showLoading?: boolean } = {}) => {
      const refreshId = ++refreshIdRef.current;
      if (showLoading) {
        setFetchState((current) => ({ status: "loading", items: current.items }));
      }
      try {
        // UX-4: PendingList aggregates active pipeline stages. Terminal audit
        // states stay out of this operational view so stale done/rejected rows
        // cannot be opened as if they still had a live preview.
        // UX-6: parallel fetch del log skip per la categoria "Ignorati".
        const [statusBuckets, nextSkipped] = await Promise.all([
          Promise.all(
            ACTIVE_INGEST_STATUSES.map((status) =>
              listIngestPending({ status, limit: ACTIVE_STATUS_FETCH_LIMIT })
            )
          ),
          listIngestSkipped({ limit: 100 }).catch((err) => {
            console.error("[ingest] listIngestSkipped failed", err);
            return [] as IngestSkipEntry[];
          }),
        ]);
        if (refreshIdRef.current !== refreshId) return;
        const next = sortNewestFirst(statusBuckets.flat());
        const nextIds = new Set(next.map((item) => item.id));
        setFetchState({ status: "ready", items: next });
        setSkippedItems(nextSkipped);
        setSelectedIds((current) => {
          const retained = new Set<string>();
          for (const id of current) {
            if (nextIds.has(id)) retained.add(id);
          }
          return retained;
        });
        setSelectedId((current) => {
          if (current && next.some((item) => item.id === current)) return current;
          return next[0]?.id ?? null;
        });
      } catch (err) {
        if (refreshIdRef.current !== refreshId) return;
        setFetchState((current) => ({
          status: "error",
          items: current.items,
          error: err instanceof Error ? err.message : "Failed to load ingest queue",
        }));
      }
    },
    []
  );

  useEffect(() => {
    void refresh({ showLoading: true });
  }, [refresh]);

  useEffect(() => {
    const handler = () => {
      if (!isOnIngestRoute()) return;
      void refresh();
    };
    window.addEventListener("marvisx:ingest_changed", handler);
    window.addEventListener("focus", handler);
    return () => {
      window.removeEventListener("marvisx:ingest_changed", handler);
      window.removeEventListener("focus", handler);
    };
  }, [refresh]);

  useEffect(() => {
    if (
      !selected ||
      previewKind(selected) !== "markdown" ||
      usesExtractedTextPreview(selected)
    ) {
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

  const runApprove = useCallback(
    async (item: IngestPendingItem) => {
      await approveIngestPending(item.id);
      await refresh();
    },
    [refresh]
  );

  const runReject = useCallback(
    async (item: IngestPendingItem) => {
      await rejectIngestPending(item.id);
      await refresh();
    },
    [refresh]
  );

  const runRetryParse = useCallback(
    async (item: IngestPendingItem) => {
      await retryParseIngestPending(item.id);
      await refresh();
    },
    [refresh]
  );

  const toggleSelection = useCallback((id: string, selectedNext: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (selectedNext) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const toggleGroupSelection = useCallback((ids: string[], selectedNext: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (selectedNext) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => {
    setSelectedIds(new Set());
    setBulkNotice(null);
  }, []);

  const runBulkAction = useCallback(
    async (action: BulkAction) => {
      const candidates = selectedItems.filter((item) => canRunBulkAction(action, item));
      if (candidates.length === 0) {
        setBulkNotice({
          tone: "error",
          message: `Nessuna riga selezionata valida per ${BULK_ACTION_LABEL[action]}.`,
        });
        return;
      }

      setBulkRunning(action);
      setBulkNotice({
        tone: "info",
        message: `${BULK_ACTION_LABEL[action]} in corso su ${candidates.length} righe...`,
      });

      const failed = new Map<string, string>();
      for (const item of candidates) {
        try {
          if (action === "reparse") await retryParseIngestPending(item.id);
          else if (action === "approve") await approveIngestPending(item.id);
          else await deleteIngestPending(item.id);
        } catch (err) {
          const reason = err instanceof Error ? err.message : "unknown error";
          failed.set(item.id, reason);
        }
      }

      setSelectedIds((current) => {
        const next = new Set(current);
        for (const item of candidates) {
          if (!failed.has(item.id)) next.delete(item.id);
        }
        return next;
      });
      setBulkRunning(null);
      setBulkDeleteOpen(false);
      setBulkNotice(
        failed.size === 0
          ? {
              tone: "success",
              message: `${BULK_ACTION_LABEL[action]} completato su ${candidates.length} righe.`,
            }
          : {
              tone: "error",
              message: `${BULK_ACTION_LABEL[action]}: ${candidates.length - failed.size}/${candidates.length} completate, ${failed.size} fallite.`,
            }
      );
      await refresh();
    },
    [refresh, selectedItems]
  );

  if (fetchState.status === "loading" && items.length === 0) {
    return <LoadingState />;
  }

  return (
    <main className="flex h-full min-h-0 flex-col bg-pir-base text-pir-text-primary">
      <header className="shrink-0 border-b border-pir bg-pir-surface-0 px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h1 className="font-display text-heading text-pir-text-primary">
              File ingest triage
            </h1>
            <p className="mt-1 font-mono text-caption text-pir-text-tertiary">
              {items.length} active rows · pipeline view
            </p>
          </div>
          <div className="ml-auto">
            <AutoApprovedCounter />
          </div>
          <div className="basis-full lg:basis-auto flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setSelectorOpen(true)}
              className="inline-flex h-8 items-center gap-1.5 rounded-sm border border-pir bg-pir-surface-1 px-2.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-accent focus:border-pir-accent focus:outline-none"
              data-testid="ingest-project-selector"
              aria-label={
                selectedProjectSlug
                  ? `Project: ${selectedProjectSlug}`
                  : "Select upload project"
              }
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 16 16"
                className="h-3.5 w-3.5"
              >
                <path
                  fill="currentColor"
                  d="M1.5 4.25A1.25 1.25 0 0 1 2.75 3h3.1l1.2 1.2h6.2a1.25 1.25 0 0 1 1.25 1.25v6.3A1.25 1.25 0 0 1 13.25 13H2.75a1.25 1.25 0 0 1-1.25-1.25v-7.5Zm1.25.25a.25.25 0 0 0-.25.25v7a.25.25 0 0 0 .25.25h10.5a.25.25 0 0 0 .25-.25v-6.3a.25.25 0 0 0-.25-.25H6.64L5.44 4H2.75Z"
                />
              </svg>
              <span className="truncate normal-case">
                Project:{" "}
                <span className="text-pir-text-primary">
                  {selectedProjectSlug || (uploadProjectsLoading ? "loading" : "select")}
                </span>
              </span>
            </button>
            {selectedProjectSlug ? (
              <FolderUpload
                projectSlug={selectedProjectSlug}
                onUploaded={() => void refresh({ showLoading: true })}
              />
            ) : (
              <div className="flex min-w-[280px] items-center rounded-sm border border-pir bg-pir-surface-1 px-3 py-2 font-mono text-[10px] text-pir-text-tertiary">
                {uploadProjectsError ?? "select project to upload"}
              </div>
            )}
          </div>
        </div>
      </header>

      {selectorOpen && (
        <ProjectSelectorModal
          currentSlug={selectedProjectSlug}
          onSubmit={(slug) => {
            setSelectedProjectSlug(
              isVisibleUploadProject(slug, uploadProjects) ? slug : ""
            );
            setSelectorOpen(false);
          }}
          onClose={() => setSelectorOpen(false)}
          filter={(project) => uploadProjectSlugs.has(project.slug)}
        />
      )}

      {fetchState.status === "error" && (
        <div className="shrink-0 border-b border-pir-error bg-pir-error/10 px-4 py-2 font-mono text-caption text-pir-error" role="alert">
          {fetchState.error}
        </div>
      )}

      {selectedItems.length > 0 && (
        <BulkActionsBar
          selectedCount={selectedItems.length}
          reparseCount={reparseCount}
          approveCount={approveCount}
          deletingCount={selectedItems.length}
          running={bulkRunning}
          notice={bulkNotice}
          onReparse={() => void runBulkAction("reparse")}
          onDelete={() => setBulkDeleteOpen(true)}
          onApprove={() => void runBulkAction("approve")}
          onClear={clearSelection}
        />
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
              onApprove={runApprove}
              onReject={runReject}
              onRetryParse={runRetryParse}
              onRefresh={() => void refresh({ showLoading: false })}
            />
          ) : (
            <EmptyState />
          )}
        </div>
      </section>

      {bulkDeleteOpen && (
        <BulkDeleteDialog
          count={selectedItems.length}
          busy={bulkRunning === "delete"}
          onCancel={() => setBulkDeleteOpen(false)}
          onConfirm={() => void runBulkAction("delete")}
        />
      )}

      <ConnectionErrorToast visible={!wsConnected} />
    </main>
  );
}

function BulkActionsBar({
  selectedCount,
  reparseCount,
  approveCount,
  deletingCount,
  running,
  notice,
  onReparse,
  onDelete,
  onApprove,
  onClear,
}: {
  selectedCount: number;
  reparseCount: number;
  approveCount: number;
  deletingCount: number;
  running: BulkAction | null;
  notice: BulkNotice;
  onReparse: () => void;
  onDelete: () => void;
  onApprove: () => void;
  onClear: () => void;
}) {
  const busy = running !== null;
  const noticeClass = bulkNoticeClass(notice);
  return (
    <section className="shrink-0 border-b border-pir bg-pir-surface-1 px-4 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-sm border border-pir-accent/40 bg-pir-accent/10 px-2 py-1 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-accent">
          {selectedCount} selected
        </span>
        <button
          type="button"
          disabled={busy || reparseCount === 0}
          onClick={onReparse}
          className="h-7 rounded-sm border border-pir bg-pir-surface-0 px-2.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-accent disabled:cursor-not-allowed disabled:opacity-40 focus:border-pir-accent focus:outline-none"
        >
          Reparse {reparseCount}
        </button>
        <button
          type="button"
          disabled={busy || deletingCount === 0}
          onClick={onDelete}
          className="h-7 rounded-sm border border-pir-error/40 bg-transparent px-2.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-error transition-colors hover:border-pir-error hover:bg-pir-error/10 disabled:cursor-not-allowed disabled:opacity-40 focus:border-pir-accent focus:outline-none"
        >
          Delete {deletingCount}
        </button>
        <button
          type="button"
          disabled={busy || approveCount === 0}
          onClick={onApprove}
          className="h-7 rounded-sm border border-pir-accent bg-pir-accent px-2.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-base transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40 focus:border-pir-accent focus:outline-none"
        >
          Approve {approveCount}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onClear}
          className="h-7 rounded-sm border border-transparent px-2.5 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:text-pir-text-primary disabled:cursor-not-allowed disabled:opacity-40 focus:outline-none"
        >
          Clear
        </button>
        {notice && (
          <span className={`min-w-0 truncate font-mono text-[10px] ${noticeClass}`}>
            {notice.message}
          </span>
        )}
      </div>
    </section>
  );
}

function BulkDeleteDialog({
  count,
  busy,
  onCancel,
  onConfirm,
}: {
  count: number;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="bulk-delete-title"
      aria-describedby="bulk-delete-description"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={busy ? undefined : onCancel}
    >
      <div
        onClick={(event) => event.stopPropagation()}
        className="w-full max-w-md rounded-sm border border-pir bg-pir-surface-0 shadow-2xl"
      >
        <header className="border-b border-pir px-5 py-3">
          <h2
            id="bulk-delete-title"
            className="font-display text-[16px] font-bold text-pir-text-primary"
          >
            Eliminare righe selezionate
          </h2>
        </header>
        <div className="space-y-3 px-5 py-4 font-mono text-caption text-pir-text-secondary">
          <p id="bulk-delete-description">
            Verranno rimossi {count} record ingest e i file fisici associati quando
            ancora presenti. Operazione irreversibile.
          </p>
        </div>
        <footer className="flex justify-end gap-2 border-t border-pir px-5 py-3">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="h-8 rounded-sm border border-pir bg-transparent px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary transition-colors hover:border-pir-strong hover:text-pir-text-primary disabled:cursor-wait disabled:opacity-50 focus:border-pir-accent focus:outline-none"
          >
            Annulla
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className="h-8 rounded-sm border border-pir-error bg-pir-error px-3 font-mono text-caption font-semibold uppercase tracking-[0.08em] text-pir-base transition-opacity hover:opacity-90 disabled:cursor-wait disabled:opacity-50 focus:border-pir-error focus:outline-none"
          >
            {busy ? "Elimino..." : "Elimina"}
          </button>
        </footer>
      </div>
    </div>
  );
}
