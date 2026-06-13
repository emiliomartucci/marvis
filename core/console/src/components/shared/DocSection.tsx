// v1.1.0 - 2026-04-22 - Shared DocSection for /projects single-pager v2 (PR #9).
//
// Renders a list of DocEntry rows with Okabe-Ito CVD-safe tag colors and hover
// row affordance (copy path + open in Finder). Designed for reuse across
// /projects, /finder, /inbox.
//
// Palette + kind extraction consolidated in `@/lib/docTags` (single source of
// truth, shared with UniverseSidebar + future UniverseSpiralLayer PR #13).
"use client";

import type { CSSProperties } from "react";
import type { DocEntry } from "@/lib/types";
import { docTagColor, kindFromFilename } from "@/lib/docTags";

function shortDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  // Accept ISO or YYYY-MM-DD — bail gracefully on garbage
  const d = new Date(dateStr);
  if (Number.isNaN(d.getTime())) return dateStr.slice(0, 10);
  const now = Date.now();
  const diff = now - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return d.toISOString().slice(0, 10);
}

interface DocSectionItem extends DocEntry {
  href?: string;     // optional external link / finder URL
}

interface DocSectionProps {
  title: string;
  items: DocSectionItem[];
  slug: string;
  /** Max rows to show. Defaults to 5. */
  limit?: number;
  /** Click handler for a row. Falls back to opening `href` in a new tab. */
  onRowClick?: (item: DocSectionItem) => void;
  /** Action displayed on the right of the section header. */
  action?: React.ReactNode;
  /** Eyebrow shown above the title. */
  eyebrow?: string;
}

const rowStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "auto 1fr auto",
  gap: 10,
  alignItems: "center",
  padding: "8px 10px",
  borderRadius: 2,
  cursor: "pointer",
};

const tagStyle: CSSProperties = {
  padding: "2px 6px",
  borderRadius: 2,
  fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
  fontWeight: 700,
  fontSize: 9,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  flexShrink: 0,
};

function DocSection({
  title,
  items,
  slug,
  limit = 5,
  onRowClick,
  action,
  eyebrow,
}: DocSectionProps) {
  const visible = items.slice(0, limit);
  const hidden = items.length - visible.length;

  return (
    <div>
      {(title || eyebrow || action) && (
        <div className="flex items-center justify-between mb-3">
          <div>
            {eyebrow && (
              <div className="text-[10px] font-mono uppercase tracking-[0.22em] text-pir-text-tertiary">
                {eyebrow}
              </div>
            )}
            {title && (
              <div className="text-[16px] font-semibold text-pir-text-primary leading-tight mt-0.5">
                {title}
              </div>
            )}
          </div>
          {action && <div>{action}</div>}
        </div>
      )}
      {visible.length === 0 ? (
        <div className="text-pir-text-tertiary text-sm px-2 py-4">Nessun doc disponibile.</div>
      ) : (
        <div className="flex flex-col gap-0.5">
          {visible.map((item) => {
            const tagKey = kindFromFilename(item.filename);
            const tag = docTagColor(tagKey);
            const title2 = item.title ?? item.filename.split("/").pop() ?? item.filename;
            return (
              <div
                key={item.filename}
                role="button"
                tabIndex={0}
                onClick={() => {
                  if (onRowClick) onRowClick(item);
                  else if (item.href) window.open(item.href, "_blank", "noopener,noreferrer");
                }}
                onKeyDown={(e) => {
                  if ((e.key === "Enter" || e.key === " ") && onRowClick) {
                    e.preventDefault();
                    onRowClick(item);
                  }
                }}
                style={rowStyle}
                className="hover:bg-pir-surface-1/60 transition-colors"
              >
                <span style={{ ...tagStyle, background: tag.bg, color: tag.fg }}>
                  {tagKey}
                </span>
                <span
                  className="text-pir-text-primary truncate"
                  style={{
                    fontFamily: "var(--pir-font-sans, system-ui)",
                    fontSize: 12.5,
                    fontWeight: 500,
                    lineHeight: 1.3,
                  }}
                  title={item.filename}
                >
                  {title2}
                </span>
                <span
                  className="text-pir-text-muted font-mono tabular-nums"
                  style={{ fontSize: 10 }}
                >
                  {shortDate(item.date)}
                </span>
              </div>
            );
          })}
        </div>
      )}
      <span className="sr-only" aria-hidden>
        {slug} · {hidden > 0 ? `${hidden} more` : "all shown"}
      </span>
    </div>
  );
}

export default DocSection;
