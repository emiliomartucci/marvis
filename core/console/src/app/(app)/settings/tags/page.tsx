"use client";

import { useEffect, useState } from "react";
import { getTags } from "@/lib/api";
import type { TagDefinition } from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

type CategoryFilter = "all" | "layer" | "type" | "domain";

const CATEGORY_CONFIG: Record<string, { label: string; className: string }> = {
  layer:  { label: "Layer",  className: "bg-blue-500/20 text-blue-700 dark:text-blue-400 border-blue-500/30" },
  type:   { label: "Type",   className: "bg-green-500/20 text-green-700 dark:text-green-400 border-green-500/30" },
  domain: { label: "Domain", className: "bg-purple-500/20 text-purple-700 dark:text-purple-400 border-purple-500/30" },
};

const ICON_SYMBOLS: Record<string, string> = {
  monitor:  "□",
  server:   "▣",
  database: "⊗",
  bug:      "◎",
  sparkle:  "✦",
  refresh:  "↺",
  activity: "∿",
  shield:   "⊡",
  rocket:   "⊿",
};

function CategoryBadge({ category }: { category: string }) {
  const cfg = CATEGORY_CONFIG[category] ?? {
    label: category,
    className: "bg-pir-surface-3 text-pir-text-muted border-pir",
  };
  return (
    <span className={`text-caption px-2 py-0.5 rounded border ${cfg.className}`}>
      {cfg.label}
    </span>
  );
}

function ColorSwatch({ bg, text }: { bg: string; text: string }) {
  return (
    <div className="flex items-center gap-2">
      <div
        className="w-6 h-6 rounded flex-shrink-0 border border-white/10"
        style={{ backgroundColor: bg }}
        title={bg}
      />
      <div className="flex flex-col">
        <span className="text-caption text-pir-text-muted font-mono leading-tight">{bg}</span>
        <span className="text-caption font-mono leading-tight" style={{ color: text }}>{text}</span>
      </div>
    </div>
  );
}

function TagRow({ tag }: { tag: TagDefinition }) {
  return (
    <tr className="border-b border-pir last:border-b-0 group hover:bg-pir-surface-2/50 transition-colors">
      <td className="px-4 py-3">
        <div className="flex items-center gap-3">
          <div
            className="w-8 h-8 rounded flex items-center justify-center flex-shrink-0 text-sm font-mono font-semibold border border-white/10"
            style={{ backgroundColor: tag.color.bg, color: tag.color.text }}
            title={`icon: ${tag.icon}`}
          >
            {ICON_SYMBOLS[tag.icon] ?? "?"}
          </div>
          <div>
            <div className="text-body text-pir-text-primary font-medium leading-tight">{tag.label}</div>
            <div className="text-caption text-pir-text-muted font-mono">{tag.id}</div>
          </div>
        </div>
      </td>
      <td className="px-4 py-3">
        <CategoryBadge category={tag.category} />
      </td>
      <td className="px-4 py-3">
        <ColorSwatch bg={tag.color.bg} text={tag.color.text} />
      </td>
      <td className="px-4 py-3">
        <span className="text-caption text-pir-text-muted font-mono">{tag.icon}</span>
      </td>
    </tr>
  );
}

export default function TagsPage() {
  const [tags, setTags] = useState<TagDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<CategoryFilter>("all");

  useEffect(() => {
    getTags()
      .then(setTags)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "Errore caricamento"))
      .finally(() => setLoading(false));
  }, []);

  const layerCount  = tags.filter((t) => t.category === "layer").length;
  const typeCount   = tags.filter((t) => t.category === "type").length;
  const domainCount = tags.filter((t) => t.category === "domain").length;

  const filterOptions: { key: CategoryFilter; label: string; count: number }[] = [
    { key: "all",    label: "Tutti",  count: tags.length },
    { key: "layer",  label: "Layer",  count: layerCount },
    { key: "type",   label: "Type",   count: typeCount },
    { key: "domain", label: "Domain", count: domainCount },
  ];

  const filtered = filter === "all" ? tags : tags.filter((t) => t.category === filter);

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-6 max-w-4xl">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <h1 className="text-heading text-pir-text-primary">Tags</h1>
            {!loading && (
              <span className="text-caption bg-pir-surface-2 px-2 py-0.5 rounded text-pir-text-muted">
                {tags.length}
              </span>
            )}
          </div>
          <div className="text-caption text-pir-text-muted">
            Definiti in <span className="font-mono">tags.yaml</span>
          </div>
        </div>

        {loading && (
          <div className="text-body text-pir-text-muted">Caricamento...</div>
        )}

        {error && (
          <ErrorAlert message={error} className="mb-4" />
        )}

        {!loading && !error && (
          <>
            <div className="flex gap-2 mb-5">
              {filterOptions.map(({ key, label, count }) => (
                <button
                  key={key}
                  onClick={() => setFilter(key)}
                  className={`text-caption px-3 py-1.5 rounded border transition-colors ${
                    filter === key
                      ? "bg-pir-accent/20 text-pir-accent border-pir-accent/30"
                      : "bg-pir-surface-1 text-pir-text-secondary border-pir hover:border-pir-strong hover:text-pir-text-primary"
                  }`}
                >
                  {label} ({count})
                </button>
              ))}
            </div>

            <div className="bg-pir-surface-1 border border-pir rounded-lg overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-pir">
                    <th className="text-left text-label text-pir-text-muted px-4 py-3">Tag</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-32">Category</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-48">Color</th>
                    <th className="text-left text-label text-pir-text-muted px-4 py-3 w-28">Icon</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="px-4 py-16 text-center text-body text-pir-text-muted">
                        Nessun tag trovato.
                      </td>
                    </tr>
                  ) : (
                    filtered.map((tag) => (
                      <TagRow key={tag.id} tag={tag} />
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <p className="mt-4 text-caption text-pir-text-muted">
              I tag canonici sono read-only. Per aggiungere un tag, modifica{" "}
              <span className="font-mono text-pir-text-secondary">~/workspace/tags.yaml</span>.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
