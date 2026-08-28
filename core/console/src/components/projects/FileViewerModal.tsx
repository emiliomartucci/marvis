"use client";

import { useEffect, useState } from "react";
import { getProjectFile } from "@/lib/api";
import SafeMarkdown from "./SafeMarkdown";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

interface Props {
  slug: string;
  filePath: string;
  filename: string;
  onClose: () => void;
  onSaved?: () => void;
}

export default function FileViewerModal({
  slug,
  filePath,
  filename,
  onClose,
}: Props) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    getProjectFile(slug, filePath, { signal: controller.signal })
      .then((data) => {
        setContent(data.content);
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : "Failed to load file");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [slug, filePath]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(event) => event.target === event.currentTarget && onClose()}
    >
      <div className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded border border-pir bg-pir-surface-1">
        <div className="flex shrink-0 items-center justify-between border-b border-pir px-5 py-3">
          <span className="min-w-0 truncate font-mono text-caption text-pir-text-muted">
            {filename}
          </span>
          <button
            onClick={onClose}
            className="ml-2 text-lg leading-none text-pir-text-muted hover:text-pir-text-secondary"
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5">
          {loading && <div className="text-body text-pir-text-muted">Loading...</div>}
          {error && <ErrorAlert message={error} />}
          {!loading && !error && content !== null && <SafeMarkdown content={content} />}
        </div>
      </div>
    </div>
  );
}
