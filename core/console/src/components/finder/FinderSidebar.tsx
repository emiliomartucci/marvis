"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getFinderTree, getFinderPins, addFinderPin, removeFinderPin } from "@/lib/api";
import type { FinderTreeNode } from "@/lib/types";
import { useDesignV2 } from "@/lib/useDesignV2";
import FinderSidebarV2 from "./FinderSidebarV2";

interface TreeItem extends FinderTreeNode {
  children?: TreeItem[];
  expanded: boolean;
  loading: boolean;
}

interface Pin {
  id: number;
  path: string;
  label: string | null;
  position: number;
}

export default function FinderSidebar() {
  const v2 = useDesignV2();
  if (v2) return <FinderSidebarV2 />;
  return <FinderSidebarV1 />;
}

function FinderSidebarV1() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const currentPath = searchParams.get("path") || "";

  const [tree, setTree] = useState<TreeItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [pins, setPins] = useState<Pin[]>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getFinderTree("")
      .then((nodes) => {
        if (cancelled) return;
        setTree(
          nodes.map((n) => ({
            ...n,
            expanded: false,
            loading: false,
          }))
        );
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getFinderPins()
      .then((data) => {
        if (!cancelled) setPins(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const handlePinCurrent = useCallback(async () => {
    if (!currentPath) return;
    try {
      const pin = await addFinderPin(currentPath);
      setPins((prev) => [...prev, pin]);
    } catch {
      // silently ignore (e.g. already pinned)
    }
  }, [currentPath]);

  const handleRemovePin = useCallback(async (pinId: number) => {
    try {
      await removeFinderPin(pinId);
      setPins((prev) => prev.filter((p) => p.id !== pinId));
    } catch {
      // silently ignore
    }
  }, []);

  const toggleExpand = useCallback(
    async (item: TreeItem, path: TreeItem[]) => {
      if (item.expanded) {
        // Collapse
        setTree((prev) => updateNode(prev, item.path, { expanded: false }));
        return;
      }

      // Expand: load children
      setTree((prev) => updateNode(prev, item.path, { loading: true, expanded: true }));

      try {
        const children = await getFinderTree(item.path);
        setTree((prev) =>
          updateNode(prev, item.path, {
            loading: false,
            children: children.map((n) => ({
              ...n,
              expanded: false,
              loading: false,
            })),
          })
        );
      } catch {
        setTree((prev) => updateNode(prev, item.path, { loading: false }));
      }
    },
    []
  );

  const navigate = useCallback(
    (path: string) => {
      router.push(`/finder/?path=${encodeURIComponent(path)}`);
    },
    [router]
  );

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-pir">
        <button
          onClick={() => router.push("/finder/")}
          className={`text-label w-full text-left px-2 py-1 rounded transition-colors ${
            !currentPath
              ? "text-pir-accent bg-pir-accent/10"
              : "text-pir-text-secondary hover:text-pir-text-primary hover:bg-pir-surface-1"
          }`}
        >
          ~ Home
        </button>
      </div>
      {/* Preferiti */}
      {(pins.length > 0 || currentPath) && (
        <div className="px-3 py-1.5 border-b border-pir">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] font-medium text-pir-text-muted uppercase tracking-wider">Preferiti</span>
            {currentPath && !pins.find(p => p.path === currentPath) && (
              <button
                onClick={handlePinCurrent}
                className="text-[10px] text-pir-text-muted hover:text-pir-accent transition-colors"
                title="Aggiungi ai preferiti"
              >
                + pin
              </button>
            )}
          </div>
          {pins.map(pin => (
            <div key={pin.id} className="flex items-center group gap-1">
              <button
                onClick={() => navigate(pin.path)}
                className={`flex-1 flex items-center gap-1.5 px-2 py-0.5 text-caption rounded transition-colors text-left ${
                  currentPath === pin.path
                    ? "text-pir-accent bg-pir-accent/10"
                    : "text-pir-text-secondary hover:text-pir-text-primary hover:bg-pir-surface-1"
                }`}
              >
                <svg className="w-3 h-3 text-amber-400 shrink-0" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M10.868 2.884c-.321-.772-1.415-.772-1.736 0l-1.83 4.401-4.753.381c-.833.067-1.171 1.107-.536 1.651l3.62 3.102-1.106 4.637c-.194.813.691 1.456 1.405 1.02L10 15.591l4.069 2.485c.713.436 1.598-.207 1.404-1.02l-1.106-4.637 3.62-3.102c.635-.544.297-1.584-.536-1.65l-4.752-.382-1.831-4.401z" clipRule="evenodd"/>
                </svg>
                <span className="truncate">{pin.label || pin.path.split("/").filter(Boolean).pop() || pin.path}</span>
              </button>
              <button
                onClick={() => handleRemovePin(pin.id)}
                className="opacity-0 group-hover:opacity-100 text-pir-text-muted hover:text-rose-400 transition-all px-1"
                title="Rimuovi dai preferiti"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="flex-1 overflow-y-auto px-1 py-1">
        {loading ? (
          <div className="px-3 py-2 text-caption text-pir-text-muted">
            Loading...
          </div>
        ) : (
          <TreeNodeList
            items={tree}
            depth={0}
            currentPath={currentPath}
            onToggle={toggleExpand}
            onNavigate={navigate}
          />
        )}
      </div>
    </div>
  );
}

function TreeNodeList({
  items,
  depth,
  currentPath,
  onToggle,
  onNavigate,
}: {
  items: TreeItem[];
  depth: number;
  currentPath: string;
  onToggle: (item: TreeItem, path: TreeItem[]) => void;
  onNavigate: (path: string) => void;
}) {
  return (
    <>
      {items.map((item) => (
        <div key={item.path}>
          <button
            onClick={() => onNavigate(item.path)}
            className={`w-full flex items-center gap-1 px-2 py-0.5 text-caption rounded transition-colors group ${
              currentPath === item.path
                ? "text-pir-accent bg-pir-accent/10"
                : "text-pir-text-secondary hover:text-pir-text-primary hover:bg-pir-surface-1"
            }`}
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            {item.has_children ? (
              <span
                onClick={(e) => {
                  e.stopPropagation();
                  onToggle(item, []);
                }}
                className="w-4 h-4 flex items-center justify-center text-pir-text-muted hover:text-pir-text-secondary shrink-0"
              >
                {item.loading ? (
                  <span className="animate-spin text-[10px]">...</span>
                ) : (
                  <svg
                    className={`w-3 h-3 transition-transform ${
                      item.expanded ? "rotate-90" : ""
                    }`}
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path
                      fillRule="evenodd"
                      d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
                      clipRule="evenodd"
                    />
                  </svg>
                )}
              </span>
            ) : (
              <span className="w-4 shrink-0" />
            )}
            <svg
              className="w-3.5 h-3.5 text-pir-text-muted shrink-0"
              viewBox="0 0 20 20"
              fill="currentColor"
            >
              <path d="M3.75 3A1.75 1.75 0 002 4.75v3.26a3.235 3.235 0 011.75-.51h12.5c.644 0 1.245.188 1.75.51V6.75A1.75 1.75 0 0016.25 5h-4.836a.25.25 0 01-.177-.073L9.823 3.513A1.75 1.75 0 008.586 3H3.75zM3.75 9A1.75 1.75 0 002 10.75v4.5c0 .966.784 1.75 1.75 1.75h12.5A1.75 1.75 0 0018 15.25v-4.5A1.75 1.75 0 0016.25 9H3.75z" />
            </svg>
            <span className="truncate">{item.name}</span>
          </button>
          {item.expanded && item.children && (
            <TreeNodeList
              items={item.children}
              depth={depth + 1}
              currentPath={currentPath}
              onToggle={onToggle}
              onNavigate={onNavigate}
            />
          )}
        </div>
      ))}
    </>
  );
}

function updateNode(
  nodes: TreeItem[],
  targetPath: string,
  updates: Partial<TreeItem>
): TreeItem[] {
  return nodes.map((node) => {
    if (node.path === targetPath) {
      return { ...node, ...updates };
    }
    if (node.children) {
      return { ...node, children: updateNode(node.children, targetPath, updates) };
    }
    return node;
  });
}
