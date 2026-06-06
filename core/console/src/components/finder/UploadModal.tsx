"use client";

import { useCallback, useRef, useState } from "react";
import { finderUpload } from "@/lib/api";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

interface UploadModalProps {
  currentPath: string;
  onClose: () => void;
  onUploaded: () => void;
}

export default function UploadModal({
  currentPath,
  onClose,
  onUploaded,
}: UploadModalProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const dropped = Array.from(e.dataTransfer.files);
    setFiles((prev) => [...prev, ...dropped]);
  }, []);

  const handleUpload = async () => {
    if (files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      await finderUpload(currentPath, files);
      onUploaded();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-pir-surface-0 border border-pir rounded-lg w-[420px] max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 border-b border-pir">
          <h3 className="text-label text-pir-text-primary">Upload Files</h3>
          <button
            onClick={onClose}
            className="text-pir-text-muted hover:text-pir-text-secondary"
          >
            <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
              <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
            </svg>
          </button>
        </div>

        <div className="p-4 flex-1 overflow-y-auto">
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            onClick={() => inputRef.current?.click()}
            className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${
              dragOver
                ? "border-pir-accent bg-pir-accent/5"
                : "border-pir-border hover:border-pir-text-muted"
            }`}
          >
            <svg
              className="w-8 h-8 mx-auto text-pir-text-muted mb-2"
              viewBox="0 0 20 20"
              fill="currentColor"
            >
              <path d="M9.25 13.25a.75.75 0 001.5 0V4.636l2.955 3.129a.75.75 0 001.09-1.03l-4.25-4.5a.75.75 0 00-1.09 0l-4.25 4.5a.75.75 0 101.09 1.03L9.25 4.636v8.614z" />
              <path d="M3.5 12.75a.75.75 0 00-1.5 0v2.5A2.75 2.75 0 004.75 18h10.5A2.75 2.75 0 0018 15.25v-2.5a.75.75 0 00-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5z" />
            </svg>
            <p className="text-caption text-pir-text-muted">
              Drop files here or click to browse
            </p>
            <input
              ref={inputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                const selected = Array.from(e.target.files || []);
                setFiles((prev) => [...prev, ...selected]);
              }}
            />
          </div>

          {files.length > 0 && (
            <div className="mt-3 space-y-1">
              {files.map((f, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2 px-2 py-1 bg-pir-surface-1 rounded text-caption"
                >
                  <span className="text-pir-text-secondary truncate flex-1">
                    {f.name}
                  </span>
                  <span className="text-pir-text-muted shrink-0">
                    {(f.size / 1024).toFixed(0)} KB
                  </span>
                  <button
                    onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                    className="text-pir-text-muted hover:text-red-400 shrink-0"
                  >
                    x
                  </button>
                </div>
              ))}
            </div>
          )}

          {error && (
            <ErrorAlert message={error} className="mt-2" />
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-pir">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleUpload}
            disabled={files.length === 0 || uploading}
            className="px-3 py-1.5 text-caption bg-pir-accent text-white rounded hover:bg-pir-accent/90 disabled:opacity-50 transition-colors"
          >
            {uploading ? "Uploading..." : `Upload ${files.length} file${files.length !== 1 ? "s" : ""}`}
          </button>
        </div>
      </div>
    </div>
  );
}
