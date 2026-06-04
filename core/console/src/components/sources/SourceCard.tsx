"use client";

import type { InboxSource } from "@/lib/types";

type Props = {
  source: InboxSource;
  onToggleActive: () => void;
  onDelete: () => void;
};

function formatRelative(iso: string | null): string {
  if (!iso) return "mai";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "mai";
  const diff = Date.now() - date.getTime();
  const hours = Math.floor(diff / (1000 * 60 * 60));
  if (hours < 1) {
    const minutes = Math.floor(diff / (1000 * 60));
    if (minutes < 1) return "adesso";
    return `${minutes}m fa`;
  }
  if (hours < 24) return `${hours}h fa`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}g fa`;
  const months = Math.floor(days / 30);
  return `${months} mesi fa`;
}

function ScoreBadge({ score }: { score: number }) {
  const color =
    score > 5
      ? "bg-pir-success/15 text-pir-success border-pir-success/40"
      : score > 0
      ? "bg-pir-surface-2 text-pir-text-primary border-pir"
      : score > -3
      ? "bg-pir-warning/15 text-pir-warning border-pir-warning/40"
      : "bg-pir-error/15 text-pir-error border-pir-error/40";

  const formatted = `${score > 0 ? "+" : ""}${score.toFixed(1)}`;
  return (
    <div
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[11px] font-mono ${color}`}
      title={`Score aggregato: ${formatted}`}
    >
      {formatted}
    </div>
  );
}

function HealthPill({ source }: { source: InboxSource }) {
  if (!source.active) {
    return (
      <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border border-pir text-pir-text-muted bg-pir-surface-2">
        disattivata
      </span>
    );
  }
  if (source.last_fetch_error) {
    return (
      <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border border-pir-error/40 text-pir-error bg-pir-error/10">
        errore
      </span>
    );
  }
  if (!source.last_fetch_at) {
    return (
      <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border border-pir-warning/40 text-pir-warning bg-pir-warning/10">
        in attesa
      </span>
    );
  }
  return (
    <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border border-pir-success/40 text-pir-success bg-pir-success/10">
      attiva
    </span>
  );
}

export function SourceCard({ source, onToggleActive, onDelete }: Props) {
  return (
    <div className="border border-pir rounded-lg bg-pir-surface-1 p-4 space-y-3 hover:border-pir-accent/40 transition-colors flex flex-col">
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <HealthPill source={source} />
            <ScoreBadge score={source.score} />
            <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border border-pir text-pir-text-muted">
              {source.source_type}
            </span>
          </div>
          <h3 className="text-sm font-semibold text-pir-text-primary truncate" title={source.name}>
            {source.name}
          </h3>
          <div className="text-xs font-mono text-pir-text-muted truncate" title={source.source_key}>
            {source.source_key}
          </div>
        </div>
      </div>

      {/* Feed URL */}
      {source.feed_url && (
        <a
          href={source.feed_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-pir-text-muted hover:text-pir-accent truncate block"
          title={source.feed_url}
        >
          {source.feed_url}
        </a>
      )}

      {/* Metrics grid */}
      <div className="grid grid-cols-3 gap-2 text-center">
        <div>
          <div className="text-[10px] uppercase text-pir-text-muted tracking-wider">Totali</div>
          <div className="text-sm font-mono text-pir-text-primary">{source.total_items}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-pir-text-muted tracking-wider">Unread</div>
          <div className="text-sm font-mono text-pir-accent">{source.unread_count}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase text-pir-text-muted tracking-wider">Ignored</div>
          <div className="text-sm font-mono text-pir-error">{source.auto_ignored_count}</div>
        </div>
      </div>

      {/* Up/down/reads breakdown */}
      <div className="grid grid-cols-3 gap-2 text-center text-[10px] text-pir-text-muted">
        <div className="text-pir-success">↑ {source.upvotes}</div>
        <div className="text-pir-error">↓ {source.downvotes}</div>
        <div>{source.reads} letti</div>
      </div>

      {/* Last fetch */}
      <div className="text-[10px] text-pir-text-muted flex-1">
        Ultimo ricevuto: {formatRelative(source.last_fetch_at)}
        {source.last_fetch_error && (
          <div
            className="text-pir-error mt-1 line-clamp-2"
            title={source.last_fetch_error}
          >
            {source.last_fetch_error}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 pt-2 border-t border-pir">
        <button
          type="button"
          onClick={onToggleActive}
          className="flex-1 text-xs px-2 py-1 rounded border border-pir hover:bg-pir-surface-2 text-pir-text-primary transition-colors"
        >
          {source.active ? "Disattiva" : "Riattiva"}
        </button>
        <button
          type="button"
          onClick={onDelete}
          className="text-xs px-2 py-1 rounded border border-pir-error/40 text-pir-error hover:bg-pir-error/10 transition-colors"
        >
          Elimina
        </button>
      </div>
    </div>
  );
}
