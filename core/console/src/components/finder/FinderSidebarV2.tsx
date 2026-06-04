// v1.0.0 - 2026-04-22 - Theme-v2 Finder sidebar: inline filter + pinned + tree chevron + L5 footer (PR #10)
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  getFinderTree,
  getFinderPins,
  addFinderPin,
  removeFinderPin,
} from "@/lib/api";
import type { FinderTreeNode } from "@/lib/types";
import { L5Loader } from "@/components/ui/L5Loader";
import FinderContextMenu, { type ContextMenuItem } from "./FinderContextMenu";

interface TreeItem extends FinderTreeNode {
  children?: TreeItem[];
  loading: boolean;
}

interface PinDto {
  id: number;
  path: string;
  label: string | null;
  position: number;
}

const EXPANDED_KEY = "marvis-finder-expanded";
const PINNED_COLLAPSED_KEY = "marvis-finder-pinned-collapsed";

function loadSet(key: string): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}

function persistSet(key: string, value: Set<string>) {
  try {
    localStorage.setItem(key, JSON.stringify([...value]));
  } catch {
    // Safari private mode fallback — in-memory only
  }
}

function loadBool(key: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function persistBool(key: string, value: boolean) {
  try {
    localStorage.setItem(key, value ? "true" : "false");
  } catch {
    // no-op
  }
}

/** Count files + folders recursively from loaded tree (what user has seen) */
function countNodes(nodes: TreeItem[]): { folders: number; files: number } {
  let folders = 0;
  let files = 0;
  const walk = (arr: TreeItem[]) => {
    for (const n of arr) {
      if (n.has_children) folders++;
      else files++;
      if (n.children) walk(n.children);
    }
  };
  walk(nodes);
  return { folders, files };
}

interface Props {
  onContextMenuRequest?: (items: ContextMenuItem[], x: number, y: number) => void;
}

export default function FinderSidebarV2({ onContextMenuRequest }: Props) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const currentPath = searchParams.get("path") || "";

  const [tree, setTree] = useState<TreeItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [pins, setPins] = useState<PinDto[]>([]);
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(loadSet(EXPANDED_KEY));
  const [pinnedCollapsed, setPinnedCollapsed] = useState<boolean>(loadBool(PINNED_COLLAPSED_KEY));
  const [menu, setMenu] = useState<{ items: ContextMenuItem[]; x: number; y: number } | null>(null);

  // Load root tree
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getFinderTree("")
      .then((nodes) => {
        if (cancelled) return;
        setTree(nodes.map((n) => ({ ...n, loading: false })));
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Load pins
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

  const navigate = useCallback(
    (path: string) => {
      router.push(`/finder/?path=${encodeURIComponent(path)}`);
    },
    [router]
  );

  const toggleExpand = useCallback(
    async (node: TreeItem) => {
      if (expanded.has(node.path)) {
        const next = new Set(expanded);
        next.delete(node.path);
        setExpanded(next);
        persistSet(EXPANDED_KEY, next);
        return;
      }
      // Expand + lazy-load children if missing
      const next = new Set(expanded);
      next.add(node.path);
      setExpanded(next);
      persistSet(EXPANDED_KEY, next);

      if (!node.children) {
        setTree((prev) => updateNode(prev, node.path, { loading: true }));
        try {
          const children = await getFinderTree(node.path);
          setTree((prev) =>
            updateNode(prev, node.path, {
              loading: false,
              children: children.map((n) => ({ ...n, loading: false })),
            })
          );
        } catch {
          setTree((prev) => updateNode(prev, node.path, { loading: false }));
        }
      }
    },
    [expanded]
  );

  const togglePinnedCollapsed = useCallback(() => {
    setPinnedCollapsed((v) => {
      const next = !v;
      persistBool(PINNED_COLLAPSED_KEY, next);
      return next;
    });
  }, []);

  const handleAddPin = useCallback(async (path: string) => {
    try {
      const pin = await addFinderPin(path);
      setPins((prev) => [...prev.filter((p) => p.path !== path), pin]);
    } catch {
      // noop — likely already pinned
    }
  }, []);

  const handleRemovePin = useCallback(async (pinId: number) => {
    try {
      await removeFinderPin(pinId);
      setPins((prev) => prev.filter((p) => p.id !== pinId));
    } catch {
      // noop
    }
  }, []);

  const pinForPath = useCallback(
    (path: string) => pins.find((p) => p.path === path),
    [pins]
  );

  const openContextMenu = useCallback(
    (e: React.MouseEvent, node: TreeItem) => {
      e.preventDefault();
      e.stopPropagation();
      const pin = pinForPath(node.path);
      const items: ContextMenuItem[] = [
        { label: "Open", onClick: () => navigate(node.path) },
        {
          label: "Copy path",
          onClick: () => {
            try {
              navigator.clipboard.writeText(node.path);
            } catch {
              // clipboard may fail in insecure context
            }
          },
        },
        { separator: true },
        pin
          ? { label: "Unpin", onClick: () => handleRemovePin(pin.id) }
          : { label: "Pin", onClick: () => handleAddPin(node.path) },
      ];
      if (onContextMenuRequest) {
        onContextMenuRequest(items, e.clientX, e.clientY);
      } else {
        setMenu({ items, x: e.clientX, y: e.clientY });
      }
    },
    [pinForPath, handleAddPin, handleRemovePin, navigate, onContextMenuRequest]
  );

  const lowerFilter = filter.trim().toLowerCase();

  const filteredTree = useMemo(() => {
    if (!lowerFilter) return tree;
    return filterTree(tree, lowerFilter);
  }, [tree, lowerFilter]);

  // Auto-expand matched folders so filter results are visible
  const expandedForView = useMemo(() => {
    if (!lowerFilter) return expanded;
    const next = new Set(expanded);
    collectAllFolderPaths(filteredTree, next);
    return next;
  }, [filteredTree, lowerFilter, expanded]);

  const counts = countNodes(tree);

  return (
    <aside
      className="bg-pir-surface-0 border-r border-pir flex flex-col h-full shrink-0 overflow-hidden"
      style={{ width: 280 }}
    >
      {/* Search / filter bar */}
      <div className="border-b border-pir" style={{ padding: "10px 12px 8px" }}>
        <div className="relative">
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden
            className="absolute text-pir-text-muted"
            style={{ left: 6, top: "50%", transform: "translateY(-50%)" }}
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter paths…"
            className="w-full bg-pir-base border border-pir text-pir-text-secondary placeholder:text-pir-text-muted outline-none focus:border-pir-accent transition-colors"
            style={{
              fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
              fontSize: 11,
              fontWeight: 500,
              padding: "5px 8px 5px 22px",
              borderRadius: 2,
              boxSizing: "border-box",
            }}
          />
        </div>
      </div>

      {/* Pinned section (collapsible) */}
      {pins.length > 0 && (
        <div className="border-b border-pir">
          <button
            type="button"
            onClick={togglePinnedCollapsed}
            aria-expanded={!pinnedCollapsed}
            className="flex justify-between items-center w-full hover:bg-pir-surface-1/60 transition-colors text-left"
            style={{ padding: "8px 12px 6px 12px" }}
          >
            <span
              className="text-pir-accent uppercase"
              style={{
                fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                fontWeight: 600,
                fontSize: 9,
                letterSpacing: "0.22em",
                lineHeight: 1,
              }}
            >
              Starred · {Math.min(pins.length, 8)}
            </span>
            <span
              className="text-pir-text-muted"
              style={{
                fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                fontSize: 9,
                transform: pinnedCollapsed ? "rotate(0deg)" : "rotate(90deg)",
                transition: "transform 150ms",
                display: "inline-block",
                width: 9,
                textAlign: "center",
              }}
              aria-hidden
            >
              ›
            </span>
          </button>
          {!pinnedCollapsed && (
            <div style={{ padding: "0 4px 6px" }}>
              {pins.slice(0, 8).map((pin) => {
                const active = currentPath === pin.path;
                const name = pin.label || pin.path.split("/").filter(Boolean).pop() || pin.path;
                return (
                  <div
                    key={pin.id}
                    className="group flex items-center gap-1"
                    style={{ margin: "1px 4px" }}
                  >
                    <button
                      type="button"
                      onClick={() => navigate(pin.path)}
                      className={`flex-1 min-w-0 flex items-center gap-1.5 text-left transition-colors ${
                        active
                          ? "bg-pir-accent/10"
                          : "hover:bg-pir-surface-1/60"
                      }`}
                      style={{
                        padding: "4px 8px",
                        borderRadius: 2,
                        borderLeft: active ? "2px solid hsl(var(--pir-accent))" : "2px solid transparent",
                      }}
                      title={pin.path}
                    >
                      <svg
                        className="shrink-0"
                        width="10"
                        height="10"
                        viewBox="0 0 20 20"
                        fill="currentColor"
                        aria-hidden
                        style={{ color: "hsl(var(--pir-accent))" }}
                      >
                        <path
                          fillRule="evenodd"
                          d="M10.868 2.884c-.321-.772-1.415-.772-1.736 0l-1.83 4.401-4.753.381c-.833.067-1.171 1.107-.536 1.651l3.62 3.102-1.106 4.637c-.194.813.691 1.456 1.405 1.02L10 15.591l4.069 2.485c.713.436 1.598-.207 1.404-1.02l-1.106-4.637 3.62-3.102c.635-.544.297-1.584-.536-1.65l-4.752-.382-1.831-4.401z"
                          clipRule="evenodd"
                        />
                      </svg>
                      <span
                        className={`truncate ${
                          active ? "text-pir-text-primary" : "text-pir-text-secondary"
                        }`}
                        style={{
                          fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                          fontSize: 11,
                          fontWeight: active ? 600 : 500,
                        }}
                      >
                        {name}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => handleRemovePin(pin.id)}
                      className="opacity-0 group-hover:opacity-100 text-pir-text-muted hover:text-rose-400 transition-all"
                      style={{ padding: "2px 4px", fontSize: 11 }}
                      title="Unpin"
                      aria-label={`Unpin ${name}`}
                    >
                      ×
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Tree */}
      <div className="flex-1 overflow-y-auto" style={{ padding: "6px 0" }}>
        <button
          type="button"
          onClick={() => navigate("")}
          className={`w-full text-left transition-colors ${
            !currentPath
              ? "bg-pir-accent/10 text-pir-text-primary"
              : "text-pir-text-secondary hover:bg-pir-surface-1/60"
          }`}
          style={{
            padding: "5px 14px",
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 11,
            fontWeight: !currentPath ? 600 : 500,
            borderLeft: !currentPath ? "2px solid hsl(var(--pir-accent))" : "2px solid transparent",
          }}
        >
          ~ home
        </button>

        {renderTreeBody({
          loading,
          filteredTree,
          lowerFilter,
          currentPath,
          expandedForView,
          toggleExpand,
          navigate,
          openContextMenu,
        })}
      </div>

      {/* Footer: counts + loader */}
      <div
        className="border-t border-pir flex items-center justify-between text-pir-text-tertiary"
        style={{
          padding: "8px 12px",
          fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
          fontSize: 9,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
        }}
      >
        <span className="tabular-nums">
          {counts.folders} folder{counts.folders === 1 ? "" : "s"} · {counts.files} file
          {counts.files === 1 ? "" : "s"}
        </span>
        {loading && <L5Loader size={12} />}
      </div>

      {menu && (
        <FinderContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />
      )}
    </aside>
  );
}

function TreeNodeList({
  items,
  depth,
  currentPath,
  expanded,
  onToggle,
  onNavigate,
  onContextMenu,
}: {
  items: TreeItem[];
  depth: number;
  currentPath: string;
  expanded: Set<string>;
  onToggle: (item: TreeItem) => void;
  onNavigate: (path: string) => void;
  onContextMenu: (e: React.MouseEvent, node: TreeItem) => void;
}) {
  return (
    <>
      {items.map((item) => {
        const isExpanded = expanded.has(item.path);
        const active = currentPath === item.path;
        return (
          <div key={item.path}>
            <div
              role="treeitem"
              aria-expanded={item.has_children ? isExpanded : undefined}
              onContextMenu={(e) => onContextMenu(e, item)}
              className={`group flex items-center gap-1 cursor-pointer transition-colors ${
                active
                  ? "bg-pir-accent/10"
                  : "hover:bg-pir-surface-1/60"
              }`}
              style={{
                paddingLeft: `${depth * 12 + 8}px`,
                paddingRight: 8,
                paddingTop: 3,
                paddingBottom: 3,
                borderLeft: active ? "2px solid hsl(var(--pir-accent))" : "2px solid transparent",
              }}
              onClick={() => onNavigate(item.path)}
            >
              {item.has_children ? (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggle(item);
                  }}
                  className="w-4 h-4 flex items-center justify-center text-pir-text-muted hover:text-pir-text-secondary shrink-0"
                  aria-label={isExpanded ? "Collapse" : "Expand"}
                >
                  {item.loading ? (
                    <span className="text-[9px]">…</span>
                  ) : (
                    <svg
                      className="w-3 h-3 pir-tree-chevron"
                      viewBox="0 0 20 20"
                      fill="currentColor"
                      style={{
                        transform: isExpanded ? "rotate(90deg)" : "rotate(0deg)",
                        transition: "transform 150ms",
                      }}
                      aria-hidden
                    >
                      <path
                        fillRule="evenodd"
                        d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
                        clipRule="evenodd"
                      />
                    </svg>
                  )}
                </button>
              ) : (
                <span className="w-4 shrink-0" aria-hidden />
              )}
              <svg
                className="w-3.5 h-3.5 shrink-0 text-pir-text-tertiary"
                viewBox="0 0 20 20"
                fill="currentColor"
                aria-hidden
              >
                {item.has_children ? (
                  <path d="M3.75 3A1.75 1.75 0 002 4.75v3.26a3.235 3.235 0 011.75-.51h12.5c.644 0 1.245.188 1.75.51V6.75A1.75 1.75 0 0016.25 5h-4.836a.25.25 0 01-.177-.073L9.823 3.513A1.75 1.75 0 008.586 3H3.75zM3.75 9A1.75 1.75 0 002 10.75v4.5c0 .966.784 1.75 1.75 1.75h12.5A1.75 1.75 0 0018 15.25v-4.5A1.75 1.75 0 0016.25 9H3.75z" />
                ) : (
                  <path d="M3 3.5A1.5 1.5 0 014.5 2h6.879a1.5 1.5 0 011.06.44l4.122 4.12A1.5 1.5 0 0117 7.622V16.5a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 013 16.5v-13z" />
                )}
              </svg>
              <span
                className={`flex-1 min-w-0 truncate ${
                  active ? "text-pir-text-primary" : "text-pir-text-secondary"
                }`}
                style={{
                  fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
                  fontSize: 11,
                  fontWeight: active ? 600 : 500,
                }}
              >
                {item.name}
              </span>
            </div>
            {isExpanded && item.children && (
              <TreeNodeList
                items={item.children}
                depth={depth + 1}
                currentPath={currentPath}
                expanded={expanded}
                onToggle={onToggle}
                onNavigate={onNavigate}
                onContextMenu={onContextMenu}
              />
            )}
          </div>
        );
      })}
    </>
  );
}

function updateNode(
  nodes: TreeItem[],
  targetPath: string,
  updates: Partial<TreeItem>
): TreeItem[] {
  return nodes.map((node) => {
    if (node.path === targetPath) return { ...node, ...updates };
    if (node.children) {
      return { ...node, children: updateNode(node.children, targetPath, updates) };
    }
    return node;
  });
}

/** Filter tree by substring match on name or path. Keeps ancestor folders when a descendant matches. */
function filterTree(nodes: TreeItem[], query: string): TreeItem[] {
  const out: TreeItem[] = [];
  for (const n of nodes) {
    const selfMatch =
      n.name.toLowerCase().includes(query) || n.path.toLowerCase().includes(query);
    const childMatches = n.children ? filterTree(n.children, query) : [];
    if (selfMatch || childMatches.length > 0) {
      out.push({
        ...n,
        children: childMatches.length > 0 ? childMatches : n.children,
      });
    }
  }
  return out;
}

function collectAllFolderPaths(nodes: TreeItem[], into: Set<string>) {
  for (const n of nodes) {
    if (n.has_children) into.add(n.path);
    if (n.children) collectAllFolderPaths(n.children, into);
  }
}

function renderTreeBody(args: {
  loading: boolean;
  filteredTree: TreeItem[];
  lowerFilter: string;
  currentPath: string;
  expandedForView: Set<string>;
  toggleExpand: (n: TreeItem) => void;
  navigate: (path: string) => void;
  openContextMenu: (e: React.MouseEvent, n: TreeItem) => void;
}) {
  if (args.loading) {
    return (
      <div className="px-3 py-4 text-pir-text-muted text-[11px] flex items-center gap-2">
        <L5Loader size={14} />
        <span>Loading tree…</span>
      </div>
    );
  }
  if (args.filteredTree.length === 0) {
    return (
      <div className="px-3 py-4 text-pir-text-muted text-[11px]">
        {args.lowerFilter ? "No paths match." : "Empty."}
      </div>
    );
  }
  return (
    <TreeNodeList
      items={args.filteredTree}
      depth={0}
      currentPath={args.currentPath}
      expanded={args.expandedForView}
      onToggle={args.toggleExpand}
      onNavigate={args.navigate}
      onContextMenu={args.openContextMenu}
    />
  );
}
