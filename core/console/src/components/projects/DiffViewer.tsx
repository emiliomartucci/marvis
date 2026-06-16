// v1.0.0 - 2026-02-27 - Unified diff viewer using diff2html + DOMPurify
"use client";

import { useMemo } from "react";
import { html as diff2html } from "diff2html";
import DOMPurify from "dompurify";
import "diff2html/bundles/css/diff2html.min.css";

interface DiffViewerProps {
  unifiedDiff: string;
}

export default function DiffViewer({ unifiedDiff }: DiffViewerProps) {
  const safeHtml = useMemo(() => {
    if (!unifiedDiff) return "";
    const rawHtml = diff2html(unifiedDiff, {
      drawFileList: true,
      matching: "lines",
      outputFormat: "line-by-line",
    });
    return DOMPurify.sanitize(rawHtml);
  }, [unifiedDiff]);

  if (!safeHtml) {
    return (
      <div className="text-caption text-pir-text-muted py-2">No diff available</div>
    );
  }

  return (
    <div
      className="diff-viewer text-xs overflow-x-auto [&_.d2h-wrapper]:bg-transparent [&_.d2h-file-header]:bg-pir-surface-0 [&_.d2h-file-header]:border-pir [&_.d2h-code-linenumber]:bg-pir-surface-0 [&_.d2h-code-line]:bg-transparent [&_.d2h-info]:bg-pir-surface-0 [&_.d2h-file-list-wrapper]:bg-pir-surface-0 [&_.d2h-file-list-wrapper]:border-pir [&_td]:text-pir-text-secondary [&_.d2h-del]:bg-pir-error/10 [&_.d2h-ins]:bg-pir-success/10 [&_.d2h-del_.d2h-code-line-ctn]:text-pir-error [&_.d2h-ins_.d2h-code-line-ctn]:text-pir-success"
      dangerouslySetInnerHTML={{ __html: safeHtml }}
    />
  );
}
