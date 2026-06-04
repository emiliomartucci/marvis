"use client";

import { useCallback, useRef, useState } from "react";
import { uploadIngestFolder } from "@/lib/api";
import type { IngestUploadResponse } from "@/lib/types";

export type UploadItemStatus =
  | "queued"
  | "uploading"
  | "done"
  | "dedup"
  | "skipped"
  | "error"
  | "cancelled";

export interface UploadItem {
  id: string;
  file: File;
  relativePath: string;
  status: UploadItemStatus;
  error?: string;
  // UX-6: structured reason for dedup/skipped items, surfaced in the modal
  // row as a one-line subtitle ("gia presente", "mime non supportato", etc).
  detail?: string;
  response?: IngestUploadResponse;
}

export interface UploadSummary {
  total: number;
  queued: number;
  uploading: number;
  done: number;
  dedup: number;
  skipped: number;
  error: number;
  cancelled: number;
}

interface UseUploadQueueOptions {
  projectSlug: string;
  concurrency?: number;
  onItemDone?: (item: UploadItem) => void;
  onAllDone?: (items: UploadItem[]) => void;
}

const DEFAULT_CONCURRENCY = 3;

let nextItemId = 0;
function makeId(): string {
  nextItemId += 1;
  return `up-${Date.now()}-${nextItemId}`;
}

export function useUploadQueue({
  projectSlug,
  concurrency = DEFAULT_CONCURRENCY,
  onItemDone,
  onAllDone,
}: UseUploadQueueOptions) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [running, setRunning] = useState(false);
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());
  const itemsRef = useRef<UploadItem[]>([]);
  itemsRef.current = items;

  const updateItem = useCallback((id: string, patch: Partial<UploadItem>) => {
    setItems((prev) =>
      prev.map((item) => (item.id === id ? { ...item, ...patch } : item))
    );
  }, []);

  const start = useCallback(
    (candidates: Array<{ file: File; relativePath: string }>) => {
      const fresh: UploadItem[] = candidates.map((c) => ({
        id: makeId(),
        file: c.file,
        relativePath: c.relativePath,
        status: "queued" as const,
      }));
      setItems(fresh);
      itemsRef.current = fresh;
      abortControllersRef.current = new Map();
      setRunning(true);

      const queue = [...fresh];
      const workers: Promise<void>[] = [];
      const completed: UploadItem[] = [];
      const lanes = Math.max(1, Math.min(concurrency, queue.length));

      for (let i = 0; i < lanes; i += 1) {
        workers.push(
          (async () => {
            while (queue.length > 0) {
              const item = queue.shift();
              if (!item) break;
              const controller = new AbortController();
              abortControllersRef.current.set(item.id, controller);
              updateItem(item.id, { status: "uploading" });
              try {
                const response = await uploadIngestFolder(
                  projectSlug,
                  [{ file: item.file, relativePath: item.relativePath }],
                  { signal: controller.signal }
                );
                // UX-6: backend returns 200 even for silent dedup or
                // skipped (mime/path). Distinguish from a true pipeline
                // entry by inspecting the structured response payload.
                const status = classifyOutcome(response);
                const detail = describeOutcome(response, status);
                const finished: UploadItem = { ...item, status, response, detail };
                updateItem(item.id, { status, response, detail });
                completed.push(finished);
                onItemDone?.(finished);
              } catch (err) {
                const aborted = controller.signal.aborted;
                const failed: UploadItem = {
                  ...item,
                  status: aborted ? "cancelled" : "error",
                  error: err instanceof Error ? err.message : "upload failed",
                };
                updateItem(item.id, { status: failed.status, error: failed.error });
                completed.push(failed);
                console.error("[upload-queue] item failed", item.relativePath, err);
              } finally {
                abortControllersRef.current.delete(item.id);
              }
            }
          })()
        );
      }

      Promise.all(workers)
        .then(() => {
          setRunning(false);
          onAllDone?.(completed);
        })
        .catch((err) => {
          console.error("[upload-queue] worker pool error", err);
          setRunning(false);
        });
    },
    [projectSlug, concurrency, updateItem, onItemDone, onAllDone]
  );

  const cancel = useCallback((id: string) => {
    const controller = abortControllersRef.current.get(id);
    if (controller) controller.abort();
    setItems((prev) =>
      prev.map((item) => {
        if (item.id !== id) return item;
        if (item.status === "queued") return { ...item, status: "cancelled" };
        return item;
      })
    );
  }, []);

  const cancelAll = useCallback(() => {
    abortControllersRef.current.forEach((controller) => controller.abort());
    abortControllersRef.current.clear();
    setItems((prev) =>
      prev.map((item) =>
        item.status === "queued" || item.status === "uploading"
          ? { ...item, status: "cancelled" }
          : item
      )
    );
  }, []);

  const reset = useCallback(() => {
    abortControllersRef.current.forEach((controller) => controller.abort());
    abortControllersRef.current.clear();
    setItems([]);
    setRunning(false);
  }, []);

  const summary = computeSummary(items);

  return { items, running, summary, start, cancel, cancelAll, reset };
}

function computeSummary(items: UploadItem[]): UploadSummary {
  const summary: UploadSummary = {
    total: items.length,
    queued: 0,
    uploading: 0,
    done: 0,
    dedup: 0,
    skipped: 0,
    error: 0,
    cancelled: 0,
  };
  for (const item of items) {
    summary[item.status] += 1;
  }
  return summary;
}

// UX-6: read the structured upload response and decide which terminal
// status to show. Single-file fan-out means each response carries at most
// one entry across queued/skipped/dedup/uploaded — collapse to one status.
function classifyOutcome(response: IngestUploadResponse): UploadItemStatus {
  if (response.queued_items > 0) return "done";
  if (response.dedup_files.length > 0) return "dedup";
  if (response.skipped_files.length > 0) return "skipped";
  // Edge case: backend returned 200 but reported zero work in every bucket.
  // Treat as done so the modal stays optimistic; the audit log row would
  // still surface in the "Ignorati" sidebar if the backend logged it.
  return "done";
}

function describeOutcome(
  response: IngestUploadResponse,
  status: UploadItemStatus
): string | undefined {
  if (status === "dedup") return "gia presente in pipeline";
  if (status === "skipped") {
    const reason = response.skipped_files[0]?.reason;
    if (!reason) return "skipped dal server";
    if (reason === "invalid-path") return "path non valido";
    return `skipped: ${reason}`;
  }
  return undefined;
}
