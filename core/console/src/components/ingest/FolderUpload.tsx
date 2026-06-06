"use client";

import { useEffect, useRef, useState } from "react";
import type { DragEvent } from "react";
import {
  deleteIngestPending,
  preflightIngest,
  uploadIngestZip,
} from "@/lib/api";
import type { IngestUploadResponse } from "@/lib/types";
import { ConflictModal } from "./ConflictModal";
import { IngestUploadModal } from "./IngestUploadModal";
import { useUploadQueue } from "./useUploadQueue";

type UploadCandidate = {
  file: File;
  relativePath: string;
};

type BrowserFileSystemEntry = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
};

type BrowserFileSystemFileEntry = BrowserFileSystemEntry & {
  file: (
    success: (file: File) => void,
    error?: (error: DOMException) => void
  ) => void;
};

type BrowserFileSystemDirectoryEntry = BrowserFileSystemEntry & {
  createReader: () => {
    readEntries: (
      success: (entries: BrowserFileSystemEntry[]) => void,
      error?: (error: DOMException) => void
    ) => void;
  };
};

type WebkitDirectoryInput = HTMLInputElement & {
  webkitdirectory?: boolean;
  directory?: boolean;
};

type WebkitFile = File & {
  webkitRelativePath?: string;
};

interface FolderUploadProps {
  projectSlug: string;
  onUploaded: () => void;
}

export function FolderUpload({ projectSlug, onUploaded }: FolderUploadProps) {
  const folderInputRef = useRef<HTMLInputElement>(null);
  const filesInputRef = useRef<HTMLInputElement>(null);
  const zipInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [status, setStatus] = useState<string>("ready");
  const [busy, setBusy] = useState(false);
  // UX-1: dedup conflict state. Set when preflight finds an existing
  // non-rejected row for the dropped single file.
  const [conflict, setConflict] = useState<{
    file: File;
    existing: { id: string; status: string; file_path?: string };
  } | null>(null);
  // UX-5: per-file upload modal opens for any multi-file batch (or single drop
  // post-preflight). Keeps showing terminal state until user closes.
  const [modalOpen, setModalOpen] = useState(false);

  const queue = useUploadQueue({
    projectSlug,
    concurrency: 3,
    onAllDone: (items) => {
      const ok = items.filter((it) => it.status === "done").length;
      const failed = items.filter((it) => it.status === "error").length;
      const cancelled = items.filter((it) => it.status === "cancelled").length;
      const failedSegment = failed > 0 ? ` · ${failed} failed` : "";
      const cancelledSegment = cancelled > 0 ? ` · ${cancelled} cancelled` : "";
      setStatus(`${ok} uploaded${failedSegment}${cancelledSegment}`);
      if (ok > 0) onUploaded();
    },
  });

  // Window-level dragover/drop preventDefault hardening: senza questo Chrome
  // intercetta drop fuori dalla div e apre il file in una nuova tab (default
  // browser behavior). Vedi P2.E4.2 Phase 2 plan + bug session 2026-04-29.
  useEffect(() => {
    const swallow = (event: Event) => event.preventDefault();
    window.addEventListener("dragover", swallow);
    window.addEventListener("drop", swallow);
    return () => {
      window.removeEventListener("dragover", swallow);
      window.removeEventListener("drop", swallow);
    };
  }, []);

  function startBatchUpload(candidates: UploadCandidate[]) {
    if (candidates.length === 0) return;
    setStatus(`uploading ${candidates.length} file${candidates.length !== 1 ? "s" : ""}…`);
    setModalOpen(true);
    queue.start(candidates);
  }

  async function runZipUpload(task: () => Promise<IngestUploadResponse>) {
    setBusy(true);
    setStatus("uploading zip…");
    try {
      const result = await task();
      setStatus(
        `${result.uploaded_files} uploaded · ${result.queued_items} queued · ${result.skipped_files.length} skipped`
      );
      onUploaded();
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "upload failed");
    } finally {
      setBusy(false);
    }
  }

  function uploadFolderFiles(rawFiles: FileList | File[]) {
    const candidates = Array.from(rawFiles).map((file) => {
      const webkitFile = file as WebkitFile;
      return {
        file,
        relativePath: webkitFile.webkitRelativePath || file.name,
      };
    });
    if (candidates.length === 0) return;
    if (isSingleZipCandidate(candidates)) {
      void runZipUpload(() => uploadIngestZip(projectSlug, candidates[0].file));
      return;
    }
    startBatchUpload(candidates);
  }

  function uploadSingleFiles(rawFiles: FileList | null) {
    if (!rawFiles || rawFiles.length === 0) return;
    const files = Array.from(rawFiles);
    // UX-1: preflight only on single-file drops. Multi-file/folder bypass
    // (backend INSERT OR IGNORE handles dedup silently in those cases).
    if (files.length === 1) {
      void preflightAndUpload(files[0]);
      return;
    }
    startBatchUpload(files.map((file) => ({ file, relativePath: file.name })));
  }

  async function preflightAndUpload(file: File) {
    try {
      const sha256 = await computeSha256Hex(file);
      const result = await preflightIngest(sha256, projectSlug);
      if (result.exists && result.id && result.status) {
        setConflict({
          file,
          existing: {
            id: result.id,
            status: result.status,
            file_path: result.file_path,
          },
        });
        return;
      }
    } catch {
      // Preflight error: fall through to upload (backend dedup is the safety net).
    }
    startBatchUpload([{ file, relativePath: file.name }]);
  }

  async function handleConflictReplace() {
    if (!conflict) return;
    const { file, existing } = conflict;
    setConflict(null);
    try {
      await deleteIngestPending(existing.id);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "replace failed");
      return;
    }
    startBatchUpload([{ file, relativePath: file.name }]);
  }

  function uploadZipFile(rawFiles: FileList | null) {
    const archive = rawFiles?.[0];
    if (!archive) return;
    void runZipUpload(() => uploadIngestZip(projectSlug, archive));
  }

  async function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const candidates = await flattenDropItems(event.dataTransfer);
    if (candidates.length > 0) {
      if (isSingleZipCandidate(candidates)) {
        void runZipUpload(() => uploadIngestZip(projectSlug, candidates[0].file));
        return;
      }
      startBatchUpload(candidates);
      return;
    }
    uploadFolderFiles(event.dataTransfer.files);
  }

  function closeModal() {
    if (queue.running) return;
    setModalOpen(false);
    queue.reset();
  }

  return (
    <div
      onDragEnter={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      className={`flex min-w-[280px] flex-wrap items-center gap-2 rounded-sm border px-3 py-2 transition-colors ${
        dragging
          ? "border-pir-accent bg-pir-accent/10"
          : "border-pir bg-pir-surface-1"
      }`}
    >
      <input
        ref={folderInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(event) => {
          uploadFolderFiles(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
        }}
      />
      <input
        ref={filesInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(event) => {
          uploadSingleFiles(event.currentTarget.files);
          event.currentTarget.value = "";
        }}
      />
      <input
        ref={zipInputRef}
        type="file"
        accept=".zip,application/zip"
        className="hidden"
        onChange={(event) => {
          uploadZipFile(event.currentTarget.files);
          event.currentTarget.value = "";
        }}
      />
      <button
        type="button"
        disabled={busy || queue.running}
        onClick={() => filesInputRef.current?.click()}
        className="inline-flex h-8 items-center gap-1 rounded-sm border border-pir bg-pir-surface-2 px-2 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-accent focus:border-pir-accent focus:outline-none disabled:opacity-50"
      >
        <FilesGlyph />
        Files
      </button>
      <button
        type="button"
        disabled={busy || queue.running}
        onClick={() => {
          const input = folderInputRef.current as WebkitDirectoryInput | null;
          if (!input) return;
          input.webkitdirectory = true;
          input.directory = true;
          input.setAttribute("webkitdirectory", "");
          input.setAttribute("directory", "");
          input.click();
        }}
        className="inline-flex h-8 items-center gap-1 rounded-sm border border-pir bg-pir-surface-2 px-2 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-accent focus:border-pir-accent focus:outline-none disabled:opacity-50"
      >
        <FolderGlyph />
        Folder
      </button>
      <button
        type="button"
        disabled={busy || queue.running}
        onClick={() => zipInputRef.current?.click()}
        className="inline-flex h-8 items-center gap-1 rounded-sm border border-pir bg-pir-surface-2 px-2 font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-pir-text-secondary transition-colors hover:border-pir-accent hover:text-pir-accent focus:border-pir-accent focus:outline-none disabled:opacity-50"
      >
        <ArchiveGlyph />
        ZIP
      </button>
      <div className="min-w-0 flex-1 font-mono text-[10px] text-pir-text-tertiary">
        <span className="block truncate">
          {dragging ? "drop files here" : `${projectSlug} · ${status}`}
        </span>
      </div>
      {conflict && (
        <ConflictModal
          filename={conflict.file.name}
          existing={conflict.existing}
          onIgnore={() => {
            setConflict(null);
            setStatus("ignored (dedup)");
          }}
          onReplace={() => void handleConflictReplace()}
          onClose={() => setConflict(null)}
        />
      )}
      {modalOpen && (
        <IngestUploadModal
          projectSlug={projectSlug}
          items={queue.items}
          summary={queue.summary}
          running={queue.running}
          onCancelItem={queue.cancel}
          onCancelAll={queue.cancelAll}
          onClose={closeModal}
        />
      )}
    </div>
  );
}

// UX-1: SHA-256 hex via Web Crypto API. Used for pre-upload dedup probe.
async function computeSha256Hex(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function isSingleZipCandidate(candidates: UploadCandidate[]): boolean {
  if (candidates.length !== 1) return false;
  const candidate = candidates[0];
  return (
    candidate.file.name.toLowerCase().endsWith(".zip") ||
    candidate.relativePath.toLowerCase().endsWith(".zip")
  );
}

async function flattenDropItems(dataTransfer: DataTransfer): Promise<UploadCandidate[]> {
  const entries: BrowserFileSystemEntry[] = [];
  for (const item of Array.from(dataTransfer.items)) {
    const withEntry = item as DataTransferItem & { webkitGetAsEntry?: () => unknown };
    const entry = withEntry.webkitGetAsEntry?.();
    if (isBrowserFileSystemEntry(entry)) entries.push(entry);
  }

  if (entries.length === 0) return [];
  const nested = await Promise.all(entries.map((entry) => readEntry(entry, "")));
  return nested.flat();
}

function isBrowserFileSystemEntry(value: unknown): value is BrowserFileSystemEntry {
  return (
    value !== null &&
    typeof value === "object" &&
    "isFile" in value &&
    "isDirectory" in value &&
    "name" in value
  );
}

async function readEntry(
  entry: BrowserFileSystemEntry,
  parentPath: string
): Promise<UploadCandidate[]> {
  const relativePrefix = parentPath ? `${parentPath}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await readFileEntry(entry as BrowserFileSystemFileEntry);
    return [{ file, relativePath: relativePrefix }];
  }
  if (!entry.isDirectory) return [];

  const directory = entry as BrowserFileSystemDirectoryEntry;
  const children = await readAllDirectoryEntries(directory);
  const nested = await Promise.all(
    children.map((child) => readEntry(child, relativePrefix))
  );
  return nested.flat();
}

function readFileEntry(entry: BrowserFileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function readAllDirectoryEntries(
  entry: BrowserFileSystemDirectoryEntry
): Promise<BrowserFileSystemEntry[]> {
  const reader = entry.createReader();
  const allEntries: BrowserFileSystemEntry[] = [];

  while (true) {
    const chunk = await new Promise<BrowserFileSystemEntry[]>((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (chunk.length === 0) return allEntries;
    allEntries.push(...chunk);
  }
}

function FilesGlyph() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="h-3.5 w-3.5">
      <path
        fill="currentColor"
        d="M3 1.5h6.4L12 4.1v9.4a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Zm6 1.2V5h2.3L9 2.7ZM3 2.5v11h8V6H8V2.5H3Z M5 7.5h4v1H5v-1Zm0 2h4v1H5v-1Zm0 2h3v1H5v-1Z"
      />
    </svg>
  );
}

function FolderGlyph() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="h-3.5 w-3.5">
      <path
        fill="currentColor"
        d="M1.5 4.25A1.25 1.25 0 0 1 2.75 3h3.1l1.2 1.2h6.2a1.25 1.25 0 0 1 1.25 1.25v6.3A1.25 1.25 0 0 1 13.25 13H2.75a1.25 1.25 0 0 1-1.25-1.25v-7.5Zm1.25.25a.25.25 0 0 0-.25.25v7a.25.25 0 0 0 .25.25h10.5a.25.25 0 0 0 .25-.25v-6.3a.25.25 0 0 0-.25-.25H6.64L5.44 4H2.75Z"
      />
    </svg>
  );
}

function ArchiveGlyph() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="h-3.5 w-3.5">
      <path
        fill="currentColor"
        d="M4 1.5h6.4L13 4.1v10.4H4A1 1 0 0 1 3 13.5v-11a1 1 0 0 1 1-1Zm6 1.2V5h2.3L10 2.7ZM4 2.5v11h8V6h-3V2.5H4Zm1.5 4h2v1h-2v-1Zm2 1h2v1h-2v-1Zm-2 1h2v1h-2v-1Zm2 1h2v1h-2v-1Zm-2 1h2v1h-2v-1Z"
      />
    </svg>
  );
}
