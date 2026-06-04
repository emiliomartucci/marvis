"use client";

import React, { useEffect, useState } from "react";
import { getTags } from "@/lib/api";
import type { TagDefinition } from "@/lib/types";

// Module-level cache: fetched once per page load
let tagsCache: TagDefinition[] | null = null;
let tagsPromise: Promise<TagDefinition[]> | null = null;

function loadTags(): Promise<TagDefinition[]> {
  if (tagsCache) return Promise.resolve(tagsCache);
  if (!tagsPromise) {
    tagsPromise = getTags().then((data) => {
      tagsCache = data;
      return data;
    });
  }
  return tagsPromise;
}

// SVG icons — 12x12, currentColor
const TAG_ICONS: Record<string, React.ReactNode> = {
  monitor: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8M12 17v4" />
    </svg>
  ),
  server: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="2" width="20" height="8" rx="2" />
      <rect x="2" y="14" width="20" height="8" rx="2" />
      <circle cx="6" cy="6" r="1" fill="currentColor" stroke="none" />
      <circle cx="6" cy="18" r="1" fill="currentColor" stroke="none" />
    </svg>
  ),
  database: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
      <path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3" />
    </svg>
  ),
  bug: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2l1.88 1.88M14.12 3.88 16 2M9 7.13v-1a3.003 3.003 0 1 1 6 0v1" />
      <path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6z" />
      <path d="M12 20v-9M6.53 9C4.6 8.8 3 7.1 3 5M6 13H2M3 21c0-2.1 1.7-3.9 4-4M17.47 9c1.93-.2 3.53-1.9 3.53-4M18 13h4M21 21c0-2.1-1.7-3.9-4-4" />
    </svg>
  ),
  sparkle: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3l1.68 5.16a2 2 0 0 0 1.27 1.27L21 11l-6.05 1.57a2 2 0 0 0-1.27 1.27L12 21l-1.68-7.16a2 2 0 0 0-1.27-1.27L3 11l6.05-1.57a2 2 0 0 0 1.27-1.27L12 3z" />
    </svg>
  ),
  refresh: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
      <path d="M8 16H3v5" />
    </svg>
  ),
  activity: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  ),
  shield: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  ),
  rocket: (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
      <path d="m3.5 11.5 1 4 4-4" />
      <path d="M14 3c2.8 2.8 3 7 3 9s-.2 6.2-3 9l-6-6c2.8-2.8 3-7 3-9s-.2-6.2-3-9z" />
      <path d="m12.5 3.5 4 1-4 4" />
    </svg>
  ),
};

interface TagChipProps {
  tag: string;
  size?: "sm" | "xs";
}

export function TagChip({ tag, size = "xs" }: TagChipProps) {
  const [tagDefs, setTagDefs] = useState<TagDefinition[]>([]);

  useEffect(() => {
    loadTags().then(setTagDefs).catch(() => {});
  }, []);

  const def = tagDefs.find((t) => t.id === tag);

  const padClass = size === "sm" ? "px-1.5 py-0.5 text-[11px]" : "px-1 py-0.5 text-[10px]";

  if (!def) {
    // Fallback: render unstyled chip while loading or for unknown tags
    return (
      <span className={`inline-flex items-center gap-1 rounded font-mono bg-pir-surface-2 text-pir-text-muted ${padClass}`}>
        {tag}
      </span>
    );
  }

  return (
    <span
      className={`inline-flex items-center gap-1 rounded font-mono ${padClass}`}
      style={{ backgroundColor: def.color.bg, color: def.color.text }}
    >
      {TAG_ICONS[def.icon] && (
        <span className="shrink-0 opacity-80">{TAG_ICONS[def.icon]}</span>
      )}
      {def.label}
    </span>
  );
}

// Convenience: renders a list of tag chips
export function TagList({ tags, size }: { tags: string[]; size?: "sm" | "xs" }) {
  if (!tags?.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {tags.map((t) => (
        <TagChip key={t} tag={t} size={size} />
      ))}
    </div>
  );
}
