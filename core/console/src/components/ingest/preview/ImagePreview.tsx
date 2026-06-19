"use client";

import { useState } from "react";
import { getIngestPreviewUrl } from "@/lib/api";
import type { IngestPendingItem } from "@/lib/types";
import { fileLabel, formatBytes } from "../format";

export function ImagePreview({ item }: { item: IngestPendingItem }) {
  const [failed, setFailed] = useState(false);
  const previewUrl = getIngestPreviewUrl(item.id, "image");

  if (failed) {
    return (
      <div className="flex min-h-[520px] items-center justify-center px-6 text-center">
        <div className="max-w-md">
          <p className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
            Image preview
          </p>
          <h3 className="mt-2 font-display text-heading text-pir-text-primary">
            Anteprima non disponibile.
          </h3>
          <p className="mt-2 font-sans text-body text-pir-text-secondary">
            Il browser non ha renderizzato {fileLabel(item)}. Aprilo direttamente
            dal file originale.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-full flex-col items-center gap-3 overflow-auto bg-pir-surface-0 p-5">
      <div
        className="flex w-full flex-1 items-center justify-center rounded-sm border border-pir bg-pir-base p-4"
        style={{
          backgroundImage:
            "repeating-conic-gradient(hsl(var(--pir-surface-0)) 0% 25%, hsl(var(--pir-base)) 0% 50%)",
          backgroundPosition: "50%",
          backgroundSize: "16px 16px",
        }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={previewUrl}
          alt={`Anteprima ${fileLabel(item)}`}
          onError={() => setFailed(true)}
          className="max-h-[680px] max-w-full rounded-sm border border-pir bg-pir-surface-0 object-contain"
        />
      </div>
      <div className="flex flex-wrap items-center justify-center gap-3 font-mono text-caption text-pir-text-muted">
        <span>{item.mime_type ?? "image"}</span>
        <span aria-hidden="true">·</span>
        <span>{formatBytes(item.file_size_bytes)}</span>
        <span aria-hidden="true">·</span>
        <span>{fileLabel(item)}</span>
      </div>
    </div>
  );
}
