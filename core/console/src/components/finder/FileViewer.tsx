// v1.3.0 - 2026-04-22 - Polish toolbar tokens + compact back/path + copy-toast (PR #10)
"use client";

import type { FinderFileContent } from "@/lib/types";
import { finderDownload } from "@/lib/api";
import dynamic from "next/dynamic";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useGraphNodeFromPath } from "@/hooks/useGraphNodeFromPath";

const CodeEditor = dynamic(() => import("./viewers/CodeEditor"), {
  ssr: false,
  loading: () => <div className="flex-1 bg-pir-base animate-pulse" />,
});
import MarkdownViewer from "./viewers/MarkdownViewer";

type ViewerState =
  | { mode: "closed" }
  | { mode: "viewing"; file: FinderFileContent }
  | { mode: "editing"; file: FinderFileContent; dirty: boolean };

interface FileViewerProps {
  viewer: ViewerState;
  onSave: (content: string) => void;
  onClose: () => void;
  onDirtyChange: (dirty: boolean) => void;
}

const EDITABLE_EXTENSIONS = new Set([
  ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
  ".sql", ".sh", ".md", ".txt", ".toml", ".cfg", ".ini", ".env",
  ".html", ".css", ".xml", ".csv", ".rst", ".conf",
]);

const PREVIEW_EXTENSIONS = new Set([".md", ".html"]);

function getLanguage(filename: string): string {
  const ext = filename.split(".").pop()?.toLowerCase() || "";
  const map: Record<string, string> = {
    py: "python",
    ts: "typescript",
    tsx: "typescript",
    js: "javascript",
    jsx: "javascript",
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    sql: "sql",
    md: "markdown",
    sh: "shell",
    html: "html",
  };
  return map[ext] || "text";
}

function getGraphButtonTitle(state: "unknown" | "found" | "not_found"): string {
  if (state === "not_found") return "Not yet indexed in KG. Commit the file to index it.";
  if (state === "found") return "Open in Graph";
  return "Checking KG...";
}

export default function FileViewer({
  viewer,
  onSave,
  onClose,
  onDirtyChange,
}: FileViewerProps) {
  const [viewMode, setViewMode] = useState<"preview" | "code">("preview");
  const [copied, setCopied] = useState(false);
  const router = useRouter();

  const filePath = viewer.mode !== "closed" ? viewer.file.path : null;
  const { state: graphResolveState, nodeId: graphNodeId } = useGraphNodeFromPath(filePath);

  // Reset viewMode to "preview" whenever a new file is opened
  useEffect(() => {
    setViewMode("preview");
  }, [filePath]);

  if (viewer.mode === "closed") return null;

  const file = viewer.file;
  const ext = "." + (file.filename.split(".").pop()?.toLowerCase() || "");
  const isPreviewable = PREVIEW_EXTENSIONS.has(ext);
  const isMarkdown = ext === ".md";
  const isHtml = ext === ".html";
  const isEditable = EDITABLE_EXTENSIONS.has(ext) && file.encoding === "utf-8";
  const isBinary = file.encoding === "base64";

  const handleDownload = async () => {
    try {
      const blob = await finderDownload(file.path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = file.filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // Error handled by caller
    }
  };

  const handleExportPdf = () => {
    if (!file) return;
    const printWindow = window.open("", "_blank", "width=800,height=1100");
    if (!printWindow) return;

    const mdContainer = document.querySelector("[data-md-export]");
    const renderedHtml = mdContainer ? mdContainer.innerHTML : "";

    printWindow.document.write(`<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>${file.filename}</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  @page { size: A4; margin: 0; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Plus Jakarta Sans', -apple-system, sans-serif; font-size: 10pt; line-height: 1.6; color: #1a1a1a; padding: 15mm 18mm; max-width: 210mm; overflow-wrap: break-word; }
  h1 { font-size: 18pt; font-weight: 800; margin: 0 0 8px; color: #0a1628; border-bottom: 2px solid #0a1628; padding-bottom: 6px; }
  h2 { font-size: 13pt; font-weight: 700; margin: 16px 0 6px; color: #0a1628; }
  h3 { font-size: 11pt; font-weight: 600; margin: 12px 0 4px; color: #1a2332; }
  h4, h5, h6 { font-size: 10pt; font-weight: 600; margin: 10px 0 4px; }
  p { margin: 0 0 8px; }
  ul, ol { margin: 0 0 8px; padding-left: 20px; }
  li { margin: 2px 0; }
  a { color: #2563eb; text-decoration: underline; }
  code { font-family: 'JetBrains Mono', 'SF Mono', monospace; font-size: 9pt; background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }
  pre { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 4px; padding: 10px 12px; margin: 8px 0; overflow-x: auto; font-size: 8.5pt; }
  pre code { background: none; padding: 0; }
  blockquote { border-left: 3px solid #d1d5db; padding-left: 12px; margin: 8px 0; color: #4b5563; font-style: italic; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 9pt; table-layout: fixed; }
  th { background: #f8fafc; font-weight: 600; text-align: left; padding: 6px 8px; border: 1px solid #e5e7eb; overflow-wrap: break-word; word-break: break-word; }
  td { padding: 5px 8px; border: 1px solid #e5e7eb; overflow-wrap: break-word; word-break: break-word; }
  tr:nth-child(even) { background: #fafbfc; }
  hr { border: none; border-top: 1px solid #e5e7eb; margin: 16px 0; }
  img { max-width: 100%; height: auto; }
  strong { font-weight: 700; color: #0a1628; }
  .doc-meta { font-size: 8pt; color: #9ca3af; margin-bottom: 12px; }
  tr { page-break-inside: avoid; }
  h1, h2, h3 { page-break-after: avoid; }
  pre { page-break-inside: avoid; }
  blockquote { page-break-inside: avoid; }
</style>
</head><body>
<div class="doc-meta">${file.filename} — Exported from Marvis Console</div>
<div id="content">${renderedHtml}</div>
<script>
  document.fonts.ready.then(function() { setTimeout(function() { window.print(); }, 300); });
</script>
</body></html>`);
    printWindow.document.close();
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-1.5 bg-pir-surface-0 border-b border-pir shrink-0">
        <span className="text-caption text-pir-text-primary truncate flex-1">
          {file.filename}
          {viewer.mode === "editing" && viewer.dirty && (
            <span className="text-pir-accent ml-1">*</span>
          )}
        </span>

        <span className="text-caption text-pir-text-muted">
          {file.readonly ? "Read-only" : ""}
        </span>

        {/* Code / Preview toggle — only for previewable file types */}
        {isPreviewable && !isBinary && (
          <div className="flex items-center gap-0 border border-pir rounded overflow-hidden shrink-0">
            <button
              onClick={() => setViewMode("code")}
              className={`px-2 py-0.5 text-caption transition-colors ${
                viewMode === "code"
                  ? "text-pir-accent bg-pir-accent/10"
                  : "text-pir-text-muted hover:text-pir-text-secondary"
              }`}
            >
              Code
            </button>
            <button
              onClick={() => setViewMode("preview")}
              className={`px-2 py-0.5 text-caption transition-colors border-l border-pir ${
                viewMode === "preview"
                  ? "text-pir-accent bg-pir-accent/10"
                  : "text-pir-text-muted hover:text-pir-text-secondary"
              }`}
            >
              Preview
            </button>
          </div>
        )}

        <button
          onClick={handleDownload}
          className="p-1 text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          title="Download"
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
            <path d="M10.75 2.75a.75.75 0 00-1.5 0v8.614L6.295 8.235a.75.75 0 10-1.09 1.03l4.25 4.5a.75.75 0 001.09 0l4.25-4.5a.75.75 0 00-1.09-1.03l-2.955 3.129V2.75z" />
            <path d="M3.5 12.75a.75.75 0 00-1.5 0v2.5A2.75 2.75 0 004.75 18h10.5A2.75 2.75 0 0018 15.25v-2.5a.75.75 0 00-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5z" />
          </svg>
        </button>

        {isPreviewable && isMarkdown && (
          <button
            onClick={handleExportPdf}
            className="p-1 text-pir-text-muted hover:text-pir-text-secondary transition-colors"
            title="Export as PDF"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M5 2.75C5 1.784 5.784 1 6.75 1h6.5c.966 0 1.75.784 1.75 1.75v3.5h1.25a1.75 1.75 0 011.75 1.75v4.5a1.75 1.75 0 01-1.75 1.75H15v2a1.75 1.75 0 01-1.75 1.75h-6.5A1.75 1.75 0 015 15.25v-2H3.75A1.75 1.75 0 012 11.5V7a1.75 1.75 0 011.75-1.75H5v-3.5zM6.5 5.25h7V2.75a.25.25 0 00-.25-.25h-6.5a.25.25 0 00-.25.25v2.5zm-2.75 2a.25.25 0 00-.25.25v4.5c0 .138.112.25.25.25H5v-1.25A1.75 1.75 0 016.75 9.25h6.5c.966 0 1.75.784 1.75 1.75v1.25h1.25a.25.25 0 00.25-.25V7a.25.25 0 00-.25-.25H3.75zm9.75 5.75v2.25a.25.25 0 01-.25.25h-6.5a.25.25 0 01-.25-.25V11a.25.25 0 01.25-.25h6.5a.25.25 0 01.25.25v2z" clipRule="evenodd" />
            </svg>
          </button>
        )}

        {/* Open in Graph — only shown when flag is on */}
        {process.env.NEXT_PUBLIC_ENABLE_GRAPH_UX === "true" && (
          <button
            onClick={() => {
              if (graphResolveState === "found" && graphNodeId) {
                router.push(`/graph/?id=${encodeURIComponent(graphNodeId)}&view=list&tab=context`);
              }
            }}
            disabled={graphResolveState !== "found"}
            title={getGraphButtonTitle(graphResolveState)}
            className="p-1 text-pir-text-muted hover:text-pir-kg-node-file transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="5" cy="10" r="2.5" />
              <circle cx="15" cy="5" r="2.5" />
              <circle cx="15" cy="15" r="2.5" />
              <path d="M7.5 10l5-5M7.5 10l5 5" />
            </svg>
          </button>
        )}

        <button
          onClick={() => {
            try {
              navigator.clipboard.writeText(file.path);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            } catch {
              // clipboard may fail in insecure context
            }
          }}
          className="p-1 text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          title={copied ? "Copied!" : "Copy path"}
          aria-label="Copy path"
        >
          {copied ? (
            <svg className="w-3.5 h-3.5 text-pir-accent" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path
                fillRule="evenodd"
                d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
                clipRule="evenodd"
              />
            </svg>
          ) : (
            <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path d="M7 3.5A1.5 1.5 0 018.5 2h3.879a1.5 1.5 0 011.06.44l3.122 3.12A1.5 1.5 0 0117 6.622V12.5a1.5 1.5 0 01-1.5 1.5h-1v-3.379a3 3 0 00-.879-2.121L10.5 5.379A3 3 0 008.379 4.5H7v-1z" />
              <path d="M4.5 6A1.5 1.5 0 003 7.5v9A1.5 1.5 0 004.5 18h7a1.5 1.5 0 001.5-1.5v-5.879a1.5 1.5 0 00-.44-1.06L9.44 6.44A1.5 1.5 0 008.378 6H4.5z" />
            </svg>
          )}
        </button>

        <button
          onClick={onClose}
          className="p-1 text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          title="Close"
        >
          <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
            <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
          </svg>
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0 overflow-auto">
        {isBinary ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-pir-text-muted">
            <svg className="w-12 h-12" viewBox="0 0 20 20" fill="currentColor">
              <path d="M3 3.5A1.5 1.5 0 014.5 2h6.879a1.5 1.5 0 011.06.44l4.122 4.12A1.5 1.5 0 0117 7.622V16.5a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 013 16.5v-13z" />
            </svg>
            <p className="text-caption">Binary file ({file.mime_type || "unknown type"})</p>
            <button
              onClick={handleDownload}
              className="px-3 py-1.5 text-caption bg-pir-accent/10 text-pir-accent rounded hover:bg-pir-accent/20 transition-colors"
            >
              Download
            </button>
          </div>
        ) : isPreviewable && viewMode === "preview" ? (
          isMarkdown ? (
            <MarkdownViewer
              content={file.content}
              readOnly={!isEditable || file.readonly}
              onChange={() => onDirtyChange(true)}
              onSave={onSave}
            />
          ) : isHtml ? (
            <iframe
              srcDoc={file.content}
              sandbox="allow-scripts allow-same-origin"
              className="w-full h-full border-0"
              title="HTML preview"
            />
          ) : null
        ) : isEditable ? (
          <CodeEditor
            content={file.content}
            language={getLanguage(file.filename)}
            readOnly={file.readonly}
            onSave={onSave}
            onChange={() => onDirtyChange(true)}
          />
        ) : (
          <pre className="p-4 text-caption text-pir-text-secondary whitespace-pre-wrap font-mono">
            {file.content}
          </pre>
        )}
      </div>
    </div>
  );
}
