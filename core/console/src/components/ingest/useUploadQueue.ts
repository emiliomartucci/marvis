"use client";

export type UploadItemStatus = "queued" | "uploading" | "done" | "dedup" | "skipped" | "error" | "cancelled";

export interface UploadItem {
  id: string;
  file: File;
  relativePath: string;
  status: UploadItemStatus;
  error?: string;
  detail?: string;
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

export function useUploadQueue() {
  const items: UploadItem[] = [];
  const summary: UploadSummary = { total: 0, queued: 0, uploading: 0, done: 0, dedup: 0, skipped: 0, error: 0, cancelled: 0 };
  return { items, running: false, summary, start: () => undefined, cancel: () => undefined, cancelAll: () => undefined, reset: () => undefined };
}
