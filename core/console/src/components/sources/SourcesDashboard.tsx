"use client";

import { useCallback, useEffect, useState } from "react";

import {
  deleteInboxSource,
  listInboxSources,
  updateInboxSource,
} from "@/lib/api";
import type { InboxSource } from "@/lib/types";

import { AddSourceModal } from "./AddSourceModal";
import { SourceCard } from "./SourceCard";

export function SourcesDashboard() {
  const [sources, setSources] = useState<InboxSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showInactive, setShowInactive] = useState(true);

  const loadSources = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listInboxSources(!showInactive);
      setSources(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sources");
    } finally {
      setLoading(false);
    }
  }, [showInactive]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  const handleToggleActive = useCallback(
    async (source: InboxSource) => {
      try {
        await updateInboxSource(source.id, { active: !source.active });
        await loadSources();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Update failed");
      }
    },
    [loadSources]
  );

  const handleDelete = useCallback(
    async (source: InboxSource) => {
      const confirmed =
        typeof window !== "undefined" &&
        window.confirm(
          `Disattivare la sorgente "${source.name}"? Gli item esistenti restano in inbox.`
        );
      if (!confirmed) return;
      try {
        await deleteInboxSource(source.id);
        await loadSources();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Delete failed");
      }
    },
    [loadSources]
  );

  // Sort sources by score DESC (best first)
  const sortedSources = [...sources].sort(
    (a, b) => (b.score ?? 0) - (a.score ?? 0)
  );

  const activeCount = sources.filter((s) => s.active).length;

  return (
    <div className="flex flex-col h-full p-6 gap-4 bg-pir-base overflow-hidden">
      {/* Header */}
      <header className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-lg font-semibold text-pir-text-primary">Sources</h1>
          <p className="text-caption text-pir-text-muted mt-1">
            Gestione feed RSS e fonti dell&apos;inbox. {sources.length} sorgenti,{" "}
            {activeCount} attive.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-pir-text-muted cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={(e) => setShowInactive(e.target.checked)}
              className="accent-pir-accent"
            />
            Mostra disattivate
          </label>
          <button
            type="button"
            onClick={() => setShowAddModal(true)}
            className="px-4 py-2 bg-pir-accent text-white rounded hover:bg-pir-accent/90 transition-colors text-sm font-medium"
          >
            + Aggiungi fonte
          </button>
        </div>
      </header>

      {/* Error */}
      {error && (
        <div
          className="p-4 bg-pir-error/10 border border-pir-error/30 rounded text-pir-error text-sm"
          role="alert"
        >
          {error}
        </div>
      )}

      {/* Content */}
      {loading ? (
        <div className="flex-1 flex items-center justify-center text-sm text-pir-text-muted">
          Caricamento sorgenti...
        </div>
      ) : sortedSources.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-pir-text-muted gap-3">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            className="w-12 h-12 fill-current opacity-40"
            aria-hidden="true"
          >
            <path d="M6.18 15.64a2.18 2.18 0 0 1 2.18 2.18C8.36 19 7.38 20 6.18 20C5 20 4 19 4 17.82a2.18 2.18 0 0 1 2.18-2.18M4 4.44A15.56 15.56 0 0 1 19.56 20h-2.83A12.73 12.73 0 0 0 4 7.27V4.44m0 5.66a9.9 9.9 0 0 1 9.9 9.9h-2.83A7.07 7.07 0 0 0 4 12.93V10.1Z" />
          </svg>
          <p className="text-sm">Nessuna sorgente configurata</p>
          <button
            type="button"
            onClick={() => setShowAddModal(true)}
            className="text-pir-accent hover:underline text-sm"
          >
            Aggiungi la prima fonte
          </button>
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-auto">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4 gap-4">
            {sortedSources.map((source) => (
              <SourceCard
                key={source.id}
                source={source}
                onToggleActive={() => handleToggleActive(source)}
                onDelete={() => handleDelete(source)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Add modal */}
      {showAddModal && (
        <AddSourceModal
          onClose={() => setShowAddModal(false)}
          onCreated={() => {
            setShowAddModal(false);
            void loadSources();
          }}
        />
      )}
    </div>
  );
}
