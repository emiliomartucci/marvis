"use client";

import { useEffect, useState } from "react";
import { getIngestPreviewBlob, getIngestPreviewUrl } from "@/lib/api";
import type { IngestPendingItem } from "@/lib/types";
import { fileLabel, formatBytes } from "../format";

const MAX_INLINE_SIZE = 20 * 1024 * 1024;

export function PdfPreview({ item }: { item: IngestPendingItem }) {
  const [iframeError, setIframeError] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const previewUrl = getIngestPreviewUrl(item.id, "pdf");
  const fileSize = item.file_size_bytes ?? 0;
  const tooLarge = fileSize > MAX_INLINE_SIZE;

  /* eslint-disable react-you-might-not-need-an-effect/no-adjust-state-on-prop-change -- The PDF must be fetched as a credentialed blob so API anti-framing headers do not block the inline preview. */
  useEffect(() => {
    if (tooLarge) {
      setBlobUrl(null);
      setPreviewError(null);
      setIframeError(false);
      return;
    }

    const controller = new AbortController();
    let objectUrl: string | null = null;

    setBlobUrl(null);
    setPreviewError(null);
    setIframeError(false);

    getIngestPreviewBlob(item.id, "pdf", { signal: controller.signal })
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setPreviewError(err instanceof Error ? err.message : "Preview unavailable");
        setIframeError(true);
      });

    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [item.id, tooLarge]);
  /* eslint-enable react-you-might-not-need-an-effect/no-adjust-state-on-prop-change */

  if (tooLarge || iframeError) {
    return (
      <div className="flex min-h-[520px] items-center justify-center bg-pir-surface-0 px-6 text-center">
        <div className="max-w-md">
          <p className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
            PDF preview
          </p>
          <h3 className="mt-2 font-display text-heading text-pir-text-primary">
            Anteprima inline non disponibile.
          </h3>
          <p className="mt-2 font-sans text-body text-pir-text-secondary">
            {tooLarge
              ? `${fileLabel(item)} pesa ${formatBytes(fileSize)}. Soglia inline: 20 MB.`
              : previewError ?? `${fileLabel(item)} non puo' essere mostrato nel frame.`}
          </p>
          <a
            href={previewUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-5 inline-flex h-8 items-center rounded-sm border border-pir-accent bg-pir-accent/10 px-3 font-mono text-caption uppercase tracking-[0.08em] text-pir-accent transition-colors hover:bg-pir-accent/15 focus:border-pir-accent focus:outline-none"
          >
            Apri in nuova scheda
          </a>
        </div>
      </div>
    );
  }

  if (!blobUrl) {
    return (
      <div className="flex min-h-[520px] items-center justify-center bg-pir-surface-0 px-6 text-center">
        <p className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
          Caricamento anteprima PDF
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-[640px] flex-col bg-pir-surface-0">
      <div className="flex shrink-0 items-center justify-between border-b border-pir px-4 py-2">
        <div className="min-w-0">
          <p className="truncate font-mono text-caption text-pir-text-tertiary">
            {fileLabel(item)}
          </p>
        </div>
        <a
          href={previewUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 rounded-sm border border-pir bg-pir-surface-1 px-2 py-1 font-mono text-caption uppercase text-pir-text-tertiary hover:border-pir-strong hover:text-pir-text-primary focus:border-pir-accent focus:outline-none"
        >
          Open
        </a>
      </div>
      <iframe
        src={blobUrl}
        title={`PDF preview: ${fileLabel(item)}`}
        onError={() => setIframeError(true)}
        className="min-h-0 flex-1 border-0 bg-pir-surface-0"
        loading="lazy"
      />
    </div>
  );
}
