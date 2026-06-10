"use client";

import { useState, useEffect } from "react";
import SafeMarkdown from "./SafeMarkdown";
import { getProjectHandoffs } from "@/lib/api";
import type { HandoffEntry } from "@/lib/types";
import FileViewerModal from "./FileViewerModal";

export default function HandoffsTab({ slug }: { slug: string }) {
  const [handoffs, setHandoffs] = useState<HandoffEntry[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [viewingFile, setViewingFile] = useState<HandoffEntry | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    getProjectHandoffs(slug, { signal: controller.signal })
      .then(setHandoffs)
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [slug]);

  if (loading) return <div className="text-pir-text-muted text-body p-4">Loading handoffs...</div>;
  if (handoffs.length === 0) return <div className="text-pir-text-muted text-body p-4">No handoffs found.</div>;

  return (
    <>
      <div className="space-y-2">
        {handoffs.map((h) => (
          <div key={h.filename} className="bg-pir-surface-1 border border-pir rounded border-l-2 border-l-pir-accent/50 hover:border-l-pir-accent transition-colors">
            <div className="flex items-center">
              <button
                onClick={() => setExpanded(expanded === h.filename ? null : h.filename)}
                className="flex-1 text-left px-4 py-3 min-w-0"
              >
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-caption text-pir-accent/70 font-mono shrink-0">{h.date}</span>
                  {h.session != null && (
                    <span className="text-caption text-pir-text-muted font-mono">#{h.session}</span>
                  )}
                  {h.branch && (
                    <span className="text-caption text-pir-purple/70 font-mono truncate max-w-[200px]">
                      {h.branch}
                    </span>
                  )}
                  {h.tags && h.tags.length > 0 && (
                    <span className="flex gap-1 flex-wrap">
                      {h.tags.slice(0, 4).map((tag) => (
                        <span
                          key={tag}
                          className="px-1.5 py-0.5 rounded text-[10px] bg-pir-surface-2 text-pir-text-tertiary"
                        >
                          {tag}
                        </span>
                      ))}
                      {h.tags.length > 4 && (
                        <span className="text-[10px] text-pir-text-muted">+{h.tags.length - 4}</span>
                      )}
                    </span>
                  )}
                </div>
                <div className="text-body text-pir-text-secondary truncate mt-1">
                  {h.summary || h.filename}
                </div>
              </button>
              <button
                onClick={() => setViewingFile(h)}
                className="shrink-0 px-3 py-1 mr-3 text-caption text-pir-text-muted hover:text-pir-accent transition-colors"
                title="View full file"
              >
                View
              </button>
            </div>
            {expanded === h.filename && (
              <div className="px-4 pb-4 border-t border-pir space-y-2 mt-2">
                {/* Structured metadata from frontmatter */}
                {(h.session != null || h.branch || (h.tags && h.tags.length > 0)) && (
                  <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs">
                    {h.session != null && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-caption text-pir-text-muted uppercase tracking-wider">session</span>
                        <span className="font-mono text-pir-accent">#{h.session}</span>
                      </div>
                    )}
                    {h.branch && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-caption text-pir-text-muted uppercase tracking-wider">branch</span>
                        <span className="font-mono text-pir-purple">{h.branch}</span>
                      </div>
                    )}
                    {h.tags && h.tags.length > 0 && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-caption text-pir-text-muted uppercase tracking-wider">tags</span>
                        <div className="flex gap-1 flex-wrap">
                          {h.tags.map((tag) => (
                            <span key={tag} className="px-1.5 py-0.5 rounded text-[10px] bg-pir-surface-2 text-pir-text-tertiary border border-pir">
                              {tag}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
                {/* File path */}
                <div className="text-caption text-pir-text-muted font-mono">{h.filename}</div>
                {/* Full file view button */}
                <button
                  onClick={() => setViewingFile(h)}
                  className="text-caption text-pir-accent hover:text-pir-accent/80 transition-colors"
                >
                  Open full handoff
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      {viewingFile && (
        <FileViewerModal
          slug={slug}
          filePath={viewingFile.filename}
          filename={viewingFile.filename.split("/").pop() || viewingFile.filename}
          onClose={() => setViewingFile(null)}
        />
      )}
    </>
  );
}
