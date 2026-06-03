"use client";

import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import type { IngestPendingItem } from "@/lib/types";
import { fileLabel } from "../format";

interface MarkdownPreviewProps {
  item: IngestPendingItem;
  text: string;
  loading?: boolean;
}

export function MarkdownPreview({ item, text, loading = false }: MarkdownPreviewProps) {
  const source = text || item.extracted_text || "";
  const frontmatter = source.startsWith("---") ? source.split("---").slice(1, 2)[0] : "";
  const body = frontmatter ? source.slice(frontmatter.length + 6).trimStart() : source;

  if (loading) {
    return (
      <div className="space-y-3 p-5" aria-label="Caricamento markdown">
        {Array.from({ length: 10 }).map((_, index) => (
          <div
            key={index}
            className="h-3 animate-pulse rounded-sm bg-pir-surface-2"
            style={{ width: `${skeletonWidth(index)}%` }}
          />
        ))}
      </div>
    );
  }

  if (!source.trim()) {
    return (
      <div className="flex min-h-[420px] items-center justify-center px-6 text-center">
        <div>
          <p className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
            Anteprima testo
          </p>
          <p className="mt-2 font-sans text-body text-pir-text-secondary">
            Nessun testo estratto per {fileLabel(item)}.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="grid min-h-full grid-cols-1 xl:grid-cols-[minmax(0,1fr)_280px]">
      <article className="prose prose-invert max-w-none px-5 py-4 prose-headings:font-display prose-headings:text-pir-text-primary prose-p:text-pir-text-secondary prose-a:text-pir-accent prose-code:text-pir-warning prose-pre:border prose-pre:border-pir prose-pre:bg-pir-surface-0">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
          {body}
        </ReactMarkdown>
      </article>
      <aside className="border-t border-pir bg-pir-surface-0 p-4 xl:border-l xl:border-t-0">
        <p className="font-mono text-caption uppercase tracking-[0.08em] text-pir-text-tertiary">
          YAML
        </p>
        <pre className="mt-3 max-h-[360px] overflow-auto whitespace-pre-wrap rounded-sm border border-pir bg-pir-base p-3 font-mono text-caption leading-5 text-pir-text-secondary">
          {frontmatter || "frontmatter non presente"}
        </pre>
        <dl className="mt-4 space-y-3">
          <div>
            <dt className="font-mono text-caption uppercase text-pir-text-tertiary">parser</dt>
            <dd className="mt-1 font-mono text-caption text-pir-text-secondary">
              {item.parser_used ?? "-"}
            </dd>
          </div>
          <div>
            <dt className="font-mono text-caption uppercase text-pir-text-tertiary">chars</dt>
            <dd className="mt-1 font-mono text-caption tabular-nums text-pir-text-secondary">
              {source.length}
            </dd>
          </div>
        </dl>
      </aside>
    </div>
  );
}

function skeletonWidth(index: number): number {
  if (index % 3 === 0) return 72;
  if (index % 3 === 1) return 88;
  return 54;
}
