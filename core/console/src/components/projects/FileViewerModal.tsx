"use client";

import { useEffect, useState } from "react";
import { getProjectFile, updateProjectFile } from "@/lib/api";
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
  onSaved,
}: Props) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);

  const isEditable = filename.endsWith(".md");

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

  function handleEdit() {
    setEditContent(content || "");
    setEditing(true);
  }

  function handleCancel() {
    setEditing(false);
    setEditContent("");
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const result = await updateProjectFile(slug, filePath, editContent);
      setContent(result.content);
      setEditing(false);
      onSaved?.();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-pir-surface-1 border border-pir rounded w-full max-w-3xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-pir shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <span className="text-caption font-mono text-pir-text-muted truncate">
              {filename}
            </span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {isEditable && !editing && !loading && content !== null && (
              <button
                onClick={handleEdit}
                className="text-caption px-3 py-1 rounded bg-pir-surface-0 border border-pir text-pir-text-secondary hover:border-pir-accent transition-colors"
              >
                Edit
              </button>
            )}
            {editing && (
              <>
                <button
                  onClick={handleCancel}
                  className="text-caption px-3 py-1 rounded text-pir-text-muted hover:text-pir-text-secondary transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSave}
                  disabled={saving}
                  className="text-caption px-3 py-1 rounded bg-pir-accent text-white hover:bg-pir-accent/90 disabled:opacity-50 transition-colors"
                >
                  {saving ? "Saving..." : "Save"}
                </button>
              </>
            )}
            <button
              onClick={onClose}
              className="text-pir-text-muted hover:text-pir-text-secondary text-lg leading-none ml-2"
              aria-label="Close"
            >
              &times;
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-5">
          {loading && (
            <div className="text-body text-pir-text-muted">Loading...</div>
          )}

          {error && (
            <ErrorAlert message={error} />
          )}

          {!loading && !error && content !== null && !editing && (
            <SafeMarkdown content={content} />
          )}

          {editing && (
            <textarea
              value={editContent}
              onChange={(e) => setEditContent(e.target.value)}
              className="w-full h-full min-h-[400px] bg-pir-surface-0 border border-pir rounded p-4 text-body text-pir-text-primary font-mono resize-none focus:outline-none focus:border-pir-accent"
              spellCheck={false}
            />
          )}
        </div>
      </div>
    </div>
  );
}
