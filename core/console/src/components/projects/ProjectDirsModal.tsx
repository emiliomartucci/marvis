"use client";

import { useEffect, useState } from "react";
import { getProjectDirs, updateProjectDirs } from "@/lib/api";

interface Props {
  onClose: () => void;
  onSaved: () => void;
}

export default function ProjectDirsModal({ onClose, onSaved }: Props) {
  const [dirs, setDirs] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newDir, setNewDir] = useState("");

  useEffect(() => {
    getProjectDirs()
      .then((data) => setDirs(data.dirs))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const result = await updateProjectDirs(dirs);
      setDirs(result.dirs);
      onSaved();
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  function addDir() {
    const trimmed = newDir.trim();
    if (trimmed && !dirs.includes(trimmed)) {
      setDirs([...dirs, trimmed]);
      setNewDir("");
    }
  }

  function removeDir(index: number) {
    setDirs(dirs.filter((_, i) => i !== index));
  }

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-pir-elevated border border-pir-border rounded-lg p-5 w-full max-w-lg shadow-xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-pir-text-primary">
            Project Directories
          </h2>
          <button
            onClick={onClose}
            className="text-pir-text-muted hover:text-pir-text-secondary text-lg leading-none"
          >
            &times;
          </button>
        </div>

        <p className="text-xs text-pir-text-muted mb-3">
          Directories where the server looks for project folders (with .task files or context.md).
        </p>

        {loading ? (
          <div className="text-xs text-pir-text-muted py-4">Loading...</div>
        ) : (
          <>
            <div className="space-y-2 mb-3">
              {dirs.map((dir, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2 bg-pir-surface border border-pir-border rounded px-3 py-2"
                >
                  <span className="text-xs text-pir-text-primary font-mono flex-1 truncate">
                    {dir}
                  </span>
                  <button
                    onClick={() => removeDir(i)}
                    className="text-pir-text-muted hover:text-pir-error text-sm shrink-0"
                    title="Remove"
                  >
                    &times;
                  </button>
                </div>
              ))}
            </div>

            <div className="flex gap-2 mb-4">
              <input
                type="text"
                placeholder="~/path/to/projects"
                value={newDir}
                onChange={(e) => setNewDir(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addDir()}
                className="flex-1 bg-pir-surface border border-pir-border rounded px-3 py-1.5 text-xs text-pir-text-primary font-mono placeholder:text-pir-text-muted focus:outline-none focus:border-pir-accent"
              />
              <button
                onClick={addDir}
                disabled={!newDir.trim()}
                className="text-xs px-3 py-1.5 rounded bg-pir-surface border border-pir-border text-pir-text-secondary hover:border-pir-accent disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Add
              </button>
            </div>

            {error && (
              <div className="text-xs text-pir-error mb-3">{error}</div>
            )}

            <div className="flex justify-end gap-2">
              <button
                onClick={onClose}
                className="text-xs px-3 py-1.5 rounded text-pir-text-muted hover:text-pir-text-secondary transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={saving || dirs.length === 0}
                className="text-xs px-4 py-1.5 rounded bg-pir-accent text-white hover:bg-pir-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {saving ? "Saving..." : "Save & Reload"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
