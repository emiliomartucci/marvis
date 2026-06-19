// v2.0.0 - 2026-04-24 - Wired to MarkdownEditor (view/raw/wysiwyg)
"use client";

import dynamic from "next/dynamic";

const MarkdownEditor = dynamic(
  () => import("@/components/markdown/MarkdownEditor"),
  {
    ssr: false,
    loading: () => <div className="flex-1 bg-pir-base animate-pulse" />,
  },
);

interface MarkdownViewerProps {
  content: string;
  readOnly?: boolean;
  onChange?: (markdown: string) => void;
  onSave?: (markdown: string) => void;
}

export default function MarkdownViewer({
  content,
  readOnly = true,
  onChange,
  onSave,
}: MarkdownViewerProps) {
  return (
    <div data-md-export className="h-full">
      <MarkdownEditor
        content={content}
        readOnly={readOnly}
        onChange={onChange}
        onSave={onSave}
        defaultMode="view"
      />
    </div>
  );
}
