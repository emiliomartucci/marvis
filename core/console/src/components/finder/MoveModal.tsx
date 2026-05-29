"use client";

import { useCallback, useEffect, useState } from "react";
import { getFinderTree, finderMove } from "@/lib/api";
import type { FinderTreeNode } from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";

interface MoveModalProps {
  sourcePath: string;
  sourceName: string;
  onClose: () => void;
  onMoved: () => void;
}

export default function MoveModal({
  sourcePath,
  sourceName,
  onClose,
  onMoved,
}: MoveModalProps) {
  const [tree, setTree] = useState<FinderTreeNode[]>([]);
  const [selectedDest, setSelectedDest] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [children, setChildren] = useState<Record<string, FinderTreeNode[]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load root tree
  useEffect(() => {
    getFinderTree("").then(setTree).catch(() => {});
  }, []);

  const toggleExpand = useCallback(
    async (path: string) => {
      if (expanded.has(path)) {
        setExpanded((prev) => {
          const next = new Set(prev);
          next.delete(path);
          return next;
        });
      } else {
        setExpanded((prev) => new Set(prev).add(path));
        if (!children[path]) {
          const nodes = await getFinderTree(path).catch(() => []);
          setChildren((prev) => ({ ...prev, [path]: nodes }));
        }
      }
    },
    [expanded, children]
  );

  const handleMove = async () => {
    setLoading(true);
    setError(null);
    try {
      await finderMove(sourcePath, selectedDest);
      onMoved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Move failed");
    } finally {
      setLoading(false);
    }
  };

  const renderNodes = (nodes: FinderTreeNode[], depth: number) => (
    <div style={{ paddingLeft: depth * 16 }}>
      {nodes.map((node) => (
        <div key={node.path}>
          <button
            onClick={() => setSelectedDest(node.path)}
            onDoubleClick={() => toggleExpand(node.path)}
            className={`w-full flex items-center gap-1.5 px-2 py-1 text-left text-caption rounded transition-colors ${
              selectedDest === node.path
                ? "bg-pir-accent/15 text-pir-accent"
                : "text-pir-text-secondary hover:bg-pir-surface-1"
            }`}
          >
            {node.has_children && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  toggleExpand(node.path);
                }}
                className="text-pir-text-muted"
              >
                <svg
                  className={`w-3 h-3 transition-transform ${expanded.has(node.path) ? "rotate-90" : ""}`}
                  viewBox="0 0 20 20"
                  fill="currentColor"
                >
                  <path fillRule="evenodd" d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z" clipRule="evenodd" />
                </svg>
              </button>
            )}
            <svg className="w-3.5 h-3.5 text-pir-accent/50 shrink-0" viewBox="0 0 20 20" fill="currentColor">
              <path d="M3.75 3A1.75 1.75 0 002 4.75v3.26a3.235 3.235 0 011.75-.51h12.5c.644 0 1.245.188 1.75.51V6.75A1.75 1.75 0 0016.25 5h-4.836a.25.25 0 01-.177-.073L9.823 3.513A1.75 1.75 0 008.586 3H3.75z" />
              <path d="M3.75 9A1.75 1.75 0 002 10.75v4.5c0 .966.784 1.75 1.75 1.75h12.5A1.75 1.75 0 0018 15.25v-4.5A1.75 1.75 0 0016.25 9H3.75z" />
            </svg>
            <span className="truncate">{node.name}</span>
          </button>
          {expanded.has(node.path) && children[node.path] && (
            renderNodes(children[node.path], depth + 1)
          )}
        </div>
      ))}
    </div>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-pir-surface-0 border border-pir rounded-lg w-[400px] max-h-[70vh] flex flex-col">
        <div className="px-4 py-3 border-b border-pir">
          <h3 className="text-label text-pir-text-primary">
            Move &quot;{sourceName}&quot;
          </h3>
          <p className="text-caption text-pir-text-muted mt-0.5">
            Select destination folder
          </p>
        </div>

        <div className="flex-1 overflow-y-auto p-3 min-h-[200px]">
          {/* Home root option */}
          <button
            onClick={() => setSelectedDest("")}
            className={`w-full flex items-center gap-1.5 px-2 py-1 text-left text-caption rounded transition-colors ${
              selectedDest === ""
                ? "bg-pir-accent/15 text-pir-accent"
                : "text-pir-text-secondary hover:bg-pir-surface-1"
            }`}
          >
            <svg className="w-3.5 h-3.5 text-pir-accent/50 shrink-0" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M9.293 2.293a1 1 0 011.414 0l7 7A1 1 0 0117 11h-1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-3a1 1 0 00-1-1H9a1 1 0 00-1 1v3a1 1 0 01-1 1H5a1 1 0 01-1-1v-6H3a1 1 0 01-.707-1.707l7-7z" clipRule="evenodd" />
            </svg>
            <span>~ (home)</span>
          </button>
          {renderNodes(tree, 0)}
        </div>

        {error && (
          <div className="px-4 py-2">
            <ErrorAlert message={error} />
          </div>
        )}

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-pir">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleMove}
            disabled={loading}
            className="px-3 py-1.5 text-caption bg-pir-accent text-white rounded hover:bg-pir-accent/90 disabled:opacity-50 transition-colors"
          >
            {loading ? "Moving..." : "Move here"}
          </button>
        </div>
      </div>
    </div>
  );
}
