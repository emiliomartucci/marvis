"use client";

import { useState } from "react";
import type { FinderFileContent } from "@/lib/types";
import { finderDownload } from "@/lib/api";

interface FileViewerProps {
  file: FinderFileContent;
  onClose: () => void;
}

export default function FileViewer({ file, onClose }: FileViewerProps) {
  const [copied, setCopied] = useState(false);
  const isImage = file.encoding === "base64" && Boolean(file.mime_type?.startsWith("image/"));
  const isHtml = file.encoding === "utf-8" && (file.mime_type === "text/html" || file.filename.endsWith(".html"));

  async function handleDownload() {
    try {
      const blob = await finderDownload(file.path);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = file.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch {
      // The surrounding inspection surface keeps the file open on download failure.
    }
  }

  function copyPath() {
    navigator.clipboard.writeText(file.path).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  }

  return (
    <section className="flex h-full min-h-0 flex-col" aria-label="File preview">
      <header className="flex shrink-0 items-center gap-2 border-b border-pir bg-pir-surface-0 px-3 py-2">
        <div className="min-w-0 flex-1">
          <p className="truncate text-caption text-pir-text-primary">{file.filename}</p>
          <p className="truncate font-mono text-[10px] text-pir-text-tertiary">
            {file.mime_type ?? "unknown"} · {file.encoding}
          </p>
        </div>
        <button type="button" onClick={copyPath} className="rounded px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-secondary">
          {copied ? "Copied" : "Copy path"}
        </button>
        <button type="button" onClick={handleDownload} className="rounded px-2 py-1 text-caption text-pir-accent hover:bg-pir-accent/10">
          Download
        </button>
        <button type="button" onClick={onClose} className="rounded px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-secondary">
          Close
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-auto">
        {isImage ? (
          <img
            src={`data:${file.mime_type};base64,${file.content}`}
            alt={file.filename}
            className="mx-auto max-h-full max-w-full object-contain p-4"
          />
        ) : file.encoding === "base64" ? (
          <div className="flex h-full items-center justify-center p-6 text-center text-caption text-pir-text-muted">
            Binary file preview is unavailable. Use Download to inspect the original bytes.
          </div>
        ) : isHtml ? (
          <iframe srcDoc={file.content} sandbox="" className="h-full w-full border-0" title={file.filename} />
        ) : (
          <pre className="whitespace-pre-wrap break-words p-4 font-mono text-caption text-pir-text-secondary">
            {file.content}
          </pre>
        )}
      </div>
    </section>
  );
}
