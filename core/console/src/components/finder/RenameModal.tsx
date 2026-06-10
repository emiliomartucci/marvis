"use client";

import { useState } from "react";
import { finderRename } from "@/lib/api";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

interface RenameModalProps {
  path: string;
  currentName: string;
  onClose: () => void;
  onRenamed: () => void;
}

export default function RenameModal({
  path,
  currentName,
  onClose,
  onRenamed,
}: RenameModalProps) {
  const [newName, setNewName] = useState(currentName);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim() || newName === currentName) return;

    setLoading(true);
    setError(null);
    try {
      await finderRename(path, newName.trim());
      onRenamed();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rename failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <form
        onSubmit={handleSubmit}
        className="bg-pir-surface-0 border border-pir rounded-lg w-[360px]"
      >
        <div className="px-4 py-3 border-b border-pir">
          <h3 className="text-label text-pir-text-primary">Rename</h3>
        </div>
        <div className="p-4">
          <input
            autoFocus
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className="w-full px-3 py-2 bg-pir-surface-1 border border-pir rounded text-caption text-pir-text-primary focus:outline-none focus:border-pir-accent"
            placeholder="New name"
          />
          {error && <ErrorAlert message={error} className="mt-2" />}
        </div>
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-pir">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!newName.trim() || newName === currentName || loading}
            className="px-3 py-1.5 text-caption bg-pir-accent text-white rounded hover:bg-pir-accent/90 disabled:opacity-50 transition-colors"
          >
            {loading ? "Renaming..." : "Rename"}
          </button>
        </div>
      </form>
    </div>
  );
}
