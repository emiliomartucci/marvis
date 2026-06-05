"use client";

import { useEffect, useState } from "react";

import { createInboxSource } from "@/lib/api";
import type { SourceType } from "@/lib/types";

type Props = {
  onClose: () => void;
  onCreated: () => void;
};

const SOURCE_KEY_RE = /^[a-z0-9][a-z0-9._-]{0,62}$/i;

export function AddSourceModal({ onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [sourceKey, setSourceKey] = useState("");
  const [feedUrl, setFeedUrl] = useState("");
  const [sourceType, setSourceType] = useState<SourceType>("rss");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const validate = (): string | null => {
    if (!name.trim()) return "Nome richiesto";
    const key = sourceKey.trim();
    if (!key) return "Source key richiesto";
    if (!SOURCE_KEY_RE.test(key)) {
      return "Source key puo contenere solo lettere, numeri, trattini, underscore e punti";
    }
    const url = feedUrl.trim();
    if (url && !/^https?:\/\//i.test(url)) {
      return "Feed URL deve iniziare con http:// o https://";
    }
    return null;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const validationError = validate();
    if (validationError) {
      setError(validationError);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await createInboxSource({
        name: name.trim(),
        source_key: sourceKey.trim().toLowerCase(),
        feed_url: feedUrl.trim() || null,
        source_type: sourceType,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Creazione fallita");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-source-title"
    >
      <form
        onSubmit={handleSubmit}
        className="bg-pir-surface-1 border border-pir rounded-lg p-6 w-full max-w-md space-y-4 shadow-xl"
      >
        <h2
          id="add-source-title"
          className="text-lg font-semibold text-pir-text-primary"
        >
          Aggiungi sorgente
        </h2>

        {error && (
          <div
            className="p-3 bg-pir-error/10 border border-pir-error/30 rounded text-pir-error text-sm"
            role="alert"
          >
            {error}
          </div>
        )}

        <div>
          <label
            htmlFor="source-name"
            className="block text-xs text-pir-text-muted mb-1"
          >
            Nome
          </label>
          <input
            id="source-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full px-3 py-2 bg-pir-surface-0 border border-pir rounded text-sm text-pir-text-primary focus:outline-none focus:border-pir-accent"
            placeholder="Not Boring"
            autoFocus
            required
          />
        </div>

        <div>
          <label
            htmlFor="source-key"
            className="block text-xs text-pir-text-muted mb-1"
          >
            Source key{" "}
            <span className="text-[10px] text-pir-text-muted">(dominio o slug)</span>
          </label>
          <input
            id="source-key"
            type="text"
            value={sourceKey}
            onChange={(e) => setSourceKey(e.target.value)}
            className="w-full px-3 py-2 bg-pir-surface-0 border border-pir rounded text-sm text-pir-text-primary font-mono focus:outline-none focus:border-pir-accent"
            placeholder="notboring.co"
            required
          />
        </div>

        <div>
          <label
            htmlFor="source-feed-url"
            className="block text-xs text-pir-text-muted mb-1"
          >
            Feed URL{" "}
            <span className="text-[10px] text-pir-text-muted">(opzionale)</span>
          </label>
          <input
            id="source-feed-url"
            type="url"
            value={feedUrl}
            onChange={(e) => setFeedUrl(e.target.value)}
            className="w-full px-3 py-2 bg-pir-surface-0 border border-pir rounded text-sm text-pir-text-primary focus:outline-none focus:border-pir-accent"
            placeholder="https://notboring.co/feed"
          />
        </div>

        <div>
          <label
            htmlFor="source-type"
            className="block text-xs text-pir-text-muted mb-1"
          >
            Tipo
          </label>
          <select
            id="source-type"
            value={sourceType}
            onChange={(e) => setSourceType(e.target.value as SourceType)}
            className="w-full px-3 py-2 bg-pir-surface-0 border border-pir rounded text-sm text-pir-text-primary focus:outline-none focus:border-pir-accent"
          >
            <option value="rss">RSS</option>
            <option value="email">Email</option>
            <option value="manual">Manual</option>
            <option value="api">API</option>
          </select>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-4 py-2 text-sm text-pir-text-muted hover:text-pir-text-primary transition-colors"
          >
            Annulla
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="px-4 py-2 bg-pir-accent text-white rounded text-sm font-medium hover:bg-pir-accent/90 disabled:opacity-50 transition-colors"
          >
            {submitting ? "Creazione..." : "Aggiungi"}
          </button>
        </div>
      </form>
    </div>
  );
}
