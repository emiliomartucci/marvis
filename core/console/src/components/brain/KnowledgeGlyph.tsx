"use client";

// 6 knowledge form glyphs + unknown fallback (sub-05 §4.8, README §7).
// Pure text glyphs (▣ ▦ ⌘ ✶ ↘ !) rendered with the JetBrains Mono token
// — no emoji, intentionally austere to match the TE industrial palette.

/** @public */
export const KNOWLEDGE_GLYPHS: Record<string, string> = {
  adr: "▣",
  spec: "▦",
  playbook: "⌘",
  tribal_memory: "✶",
  external_update: "↘",
  claimed_decision: "!",
  unknown: "?",
};

/** @public */
export interface KnowledgeGlyphProps {
  form: string | null | undefined;
  className?: string;
  accent?: boolean;
}

export function KnowledgeGlyph({ form, className = "", accent = false }: KnowledgeGlyphProps) {
  const glyph = KNOWLEDGE_GLYPHS[form ?? "unknown"] ?? KNOWLEDGE_GLYPHS.unknown;
  const colorClass = accent || form === "claimed_decision"
    ? "text-[hsl(var(--pir-warning))]"
    : "text-pir-text-secondary";
  return (
    <span
      role="img"
      aria-label={`knowledge form ${form ?? "unknown"}`}
      className={`font-mono text-base leading-none ${colorClass} ${className}`}
    >
      {glyph}
    </span>
  );
}
