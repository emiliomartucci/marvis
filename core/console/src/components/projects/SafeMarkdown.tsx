"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

const schema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames || []),
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "pre", "code",
    "table", "thead", "tbody", "tr", "th", "td",
    "blockquote", "hr", "br",
    "strong", "em", "del", "a", "p",
    "img", "details", "summary",
  ],
  attributes: {
    ...defaultSchema.attributes,
    code: [...(defaultSchema.attributes?.code || []), "className"],
    a: [...(defaultSchema.attributes?.a || []), "href", "title", "target", "rel"],
  },
};

export default function SafeMarkdown({ content }: { content: string }) {
  return (
    <div className="prose dark:prose-invert prose-sm max-w-none prose-headings:text-pir-text-primary prose-p:text-pir-text-secondary prose-a:text-pir-accent prose-code:text-pir-accent/80 prose-pre:bg-pir-surface-1 prose-pre:border prose-pre:border-pir-border prose-li:text-pir-text-secondary prose-strong:text-pir-text-primary prose-table:text-pir-text-secondary prose-th:text-pir-text-primary prose-td:border-pir-border prose-th:border-pir-border prose-hr:border-pir-border prose-blockquote:border-pir-text-muted prose-blockquote:text-pir-text-muted">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeSanitize, schema]]}
        components={{
          a: ({ href, children, ...props }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
