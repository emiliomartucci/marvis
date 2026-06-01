// v1.0.0 - 2026-04-22 - §5 Recent docs section (PR #9)
"use client";

import { useEffect, useState } from "react";
import SectionShell from "./SectionShell";
import { getProjectDocs } from "@/lib/api";
import SharedDocSection from "@/components/shared/DocSection";
import type { DocEntry } from "@/lib/types";

function DocsSection({
  slug,
  onOpenAll,
}: {
  slug: string;
  onOpenAll: () => void;
}) {
  const [items, setItems] = useState<DocEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const ctrl = new AbortController();
    getProjectDocs(slug, { signal: ctrl.signal })
      .then((res) => {
        // Newest-first — date may be missing, fallback to filename lex desc
        const sorted = [...res].sort((a, b) => {
          const ad = a.date || "";
          const bd = b.date || "";
          if (ad && bd) return bd.localeCompare(ad);
          return b.filename.localeCompare(a.filename);
        });
        setItems(sorted);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [slug]);

  const total = items.length;
  const actionBtn = (
    <button
      type="button"
      onClick={onOpenAll}
      className="text-pir-text-tertiary hover:text-pir-accent transition-colors bg-transparent border border-pir cursor-pointer"
      style={{
        fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
        fontSize: 10,
        fontWeight: 500,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
        padding: "4px 8px",
        borderRadius: 2,
      }}
    >
      ↗ All docs
    </button>
  );

  return (
    <SectionShell
      anchorId="docs"
      eyebrow={total > 0 ? `Docs · ${total} indexed` : "Docs"}
      title="Recent · tag-filterable"
      action={actionBtn}
    >
      {loading ? (
        <div className="text-pir-text-tertiary text-sm px-2 py-4">Loading docs…</div>
      ) : (
        <>
          <SharedDocSection slug={slug} title="" items={items} limit={5} onRowClick={() => onOpenAll()} />
          {total > 5 && (
            <button
              type="button"
              onClick={onOpenAll}
              className="mt-2 text-pir-accent hover:text-pir-text-primary transition-colors bg-transparent border-0 cursor-pointer"
              style={{
                fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                fontSize: 10,
                fontWeight: 500,
                letterSpacing: "0.12em",
                textTransform: "uppercase",
                padding: "6px 2px",
              }}
            >
              + {total - 5} docs · filter by tag → Docs view
            </button>
          )}
        </>
      )}
    </SectionShell>
  );
}

export default DocsSection;
