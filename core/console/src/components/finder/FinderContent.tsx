// v1.3.0 - 2026-04-22 - v2 prop wiring + opened-file lift for subbar meta pill (PR #10)
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  getFinderList,
  getFinderFile,
  saveFinderFile,
  finderDownload,
  finderDelete,
  finderMkdir,
} from "@/lib/api";
import type { FinderListItem, FinderFileContent } from "@/lib/types";
import FinderToolbar from "./FinderToolbar";
import { ErrorAlert } from "@/components/ui/ErrorAlert";
import FileList from "./FileList";
import FileViewer from "./FileViewer";
import RenameModal from "./RenameModal";
import MoveModal from "./MoveModal";
import ConfirmModal from "./ConfirmModal";
import FinderContextMenu, { type ContextMenuItem } from "./FinderContextMenu";
import FinderEmptyViewer from "./FinderEmptyViewer";
import { useAuth } from "@/lib/auth";

interface FinderContentProps {
  /** v2 design system opt-in — hides legacy FinderToolbar (subbar takes its place) */
  v2?: boolean;
  /** Lifted state: notify parent when viewer opens/closes a file */
  onOpenedFileChange?: (file: FinderFileContent | null) => void;
  /** Notify parent that the list was mutated — parent may refetch counts */
  onListInvalidate?: () => void;
}

type ViewerState =
  | { mode: "closed" }
  | { mode: "viewing"; file: FinderFileContent }
  | { mode: "editing"; file: FinderFileContent; dirty: boolean };

type ModalState =
  | { type: "none" }
  | { type: "rename"; item: FinderListItem }
  | { type: "move"; item: FinderListItem }
  | { type: "delete"; item: FinderListItem }
  | { type: "bulkDelete"; paths: string[] }
  | { type: "newFolder" };

export default function FinderContent({
  v2 = false,
  onOpenedFileChange,
  onListInvalidate,
}: FinderContentProps = {}) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const currentPath = searchParams.get("path") || "";
  const highlightPath = searchParams.get("highlight") || null;
  const { permissions } = useAuth();

  const [items, setItems] = useState<FinderListItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [highlightedItem, setHighlightedItem] = useState<string | null>(null);
  const [selectedItem, setSelectedItem] = useState<string | null>(null);
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [viewer, setViewer] = useState<ViewerState>({ mode: "closed" });
  const [searchQuery, setSearchQuery] = useState("");
  const [modal, setModal] = useState<ModalState>({ type: "none" });
  const [newFolderName, setNewFolderName] = useState("");
  const [contextMenu, setContextMenu] = useState<
    { items: ContextMenuItem[]; x: number; y: number } | null
  >(null);
  const lastSelectedIndex = useRef<number | null>(null);

  // Highlight from GlobalSearch (?highlight=) — clears after 3s
  useEffect(() => {
    if (!highlightPath) return;
    setHighlightedItem(highlightPath);
    const timer = setTimeout(() => setHighlightedItem(null), 3000);
    return () => clearTimeout(timer);
  }, [highlightPath]);

  // Load directory listing
  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setSelectedItem(null);
    setSelectedItems(new Set());
    setViewer({ mode: "closed" });
    onOpenedFileChange?.(null);
    setSearchQuery("");

    getFinderList(currentPath)
      .then((res) => {
        if (!cancelled) setItems(res.items);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // onOpenedFileChange intentionally omitted — parent passes a stable setter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentPath]);

  // Filtered items
  const filteredItems = useMemo(() => {
    if (!searchQuery.trim()) return items;
    const q = searchQuery.toLowerCase();
    return items.filter((item) => item.name.toLowerCase().includes(q));
  }, [items, searchQuery]);

  const navigate = useCallback(
    (path: string) => {
      router.push(`/finder/?path=${encodeURIComponent(path)}`);
    },
    [router]
  );

  const handleOpen = useCallback(
    async (item: FinderListItem) => {
      if (item.is_dir) {
        navigate(item.path);
        return;
      }
      try {
        const file = await getFinderFile(item.path);
        if (file.encoding === "base64") {
          setViewer({ mode: "viewing", file });
        } else {
          setViewer({ mode: "editing", file, dirty: false });
        }
        onOpenedFileChange?.(file);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to open file");
      }
    },
    [navigate, onOpenedFileChange]
  );

  const handleSave = useCallback(
    async (content: string) => {
      if (viewer.mode !== "editing") return;
      try {
        const updated = await saveFinderFile(viewer.file.path, content);
        setViewer({ mode: "editing", file: updated, dirty: false });
        onOpenedFileChange?.(updated);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Save failed");
      }
    },
    [viewer, onOpenedFileChange]
  );

  const handleCloseViewer = useCallback(() => {
    setViewer({ mode: "closed" });
    onOpenedFileChange?.(null);
  }, [onOpenedFileChange]);

  const refreshList = useCallback(() => {
    setIsLoading(true);
    getFinderList(currentPath)
      .then((res) => setItems(res.items))
      .catch((err) => setError(err.message))
      .finally(() => {
        setIsLoading(false);
        onListInvalidate?.();
      });
  }, [currentPath, onListInvalidate]);

  // Multi-select handler
  const handleMultiSelect = useCallback(
    (path: string, e: React.MouseEvent) => {
      const index = filteredItems.findIndex((i) => i.path === path);

      if (e.shiftKey && lastSelectedIndex.current !== null) {
        // Range select
        const start = Math.min(lastSelectedIndex.current, index);
        const end = Math.max(lastSelectedIndex.current, index);
        const range = filteredItems.slice(start, end + 1).map((i) => i.path);
        setSelectedItems((prev) => {
          const next = new Set(prev);
          range.forEach((p) => next.add(p));
          return next;
        });
      } else {
        // Toggle single
        setSelectedItems((prev) => {
          const next = new Set(prev);
          if (next.has(path)) {
            next.delete(path);
          } else {
            next.add(path);
          }
          return next;
        });
      }
      lastSelectedIndex.current = index;
    },
    [filteredItems]
  );

  // Actions
  const handleDownloadItem = useCallback(async (item: FinderListItem) => {
    try {
      const blob = await finderDownload(item.path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = item.name;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      setError("Download failed");
    }
  }, []);

  const handleDeleteConfirm = useCallback(async () => {
    if (modal.type === "delete") {
      try {
        await finderDelete(modal.item.path);
        setModal({ type: "none" });
        refreshList();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Delete failed");
        setModal({ type: "none" });
      }
    } else if (modal.type === "bulkDelete") {
      try {
        for (const path of modal.paths) {
          await finderDelete(path);
        }
        setSelectedItems(new Set());
        setModal({ type: "none" });
        refreshList();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Delete failed");
        setModal({ type: "none" });
        refreshList();
      }
    }
  }, [modal, refreshList]);

  const handleBulkDownload = useCallback(async () => {
    const filePaths = Array.from(selectedItems);
    const fileItems = items.filter(
      (i) => filePaths.includes(i.path) && !i.is_dir
    );
    for (const item of fileItems) {
      await handleDownloadItem(item);
    }
  }, [selectedItems, items, handleDownloadItem]);

  const handleListContextMenu = useCallback(
    (e: React.MouseEvent, item: FinderListItem) => {
      e.preventDefault();
      const items: ContextMenuItem[] = [
        { label: "Open", onClick: () => handleOpen(item) },
        {
          label: "Copy path",
          onClick: () => {
            try {
              navigator.clipboard.writeText(item.path);
            } catch {
              // clipboard may fail in insecure contexts
            }
          },
        },
        {
          label: "Download",
          disabled: item.is_dir,
          onClick: () => handleDownloadItem(item),
        },
        { separator: true },
        {
          label: "Rename…",
          disabled: !permissions.canWrite,
          onClick: () => setModal({ type: "rename", item }),
        },
        {
          label: "Move…",
          disabled: !permissions.canWrite,
          onClick: () => setModal({ type: "move", item }),
        },
        { separator: true },
        {
          label: "Delete…",
          danger: true,
          disabled: !permissions.canAdmin,
          onClick: () => setModal({ type: "delete", item }),
        },
      ];
      setContextMenu({ items, x: e.clientX, y: e.clientY });
    },
    [handleOpen, handleDownloadItem, permissions.canWrite, permissions.canAdmin]
  );

  const handleNewFolder = useCallback(async () => {
    if (!newFolderName.trim()) return;
    try {
      const folderPath = currentPath
        ? `${currentPath}/${newFolderName.trim()}`
        : newFolderName.trim();
      await finderMkdir(folderPath);
      setModal({ type: "none" });
      setNewFolderName("");
      refreshList();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create folder");
      setModal({ type: "none" });
    }
  }, [newFolderName, currentPath, refreshList]);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {v2 ? (
        <FinderV2ActionsBar
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          selectedCount={selectedItems.size}
          canWrite={permissions.canWrite}
          canAdmin={permissions.canAdmin}
          onRefresh={refreshList}
          onNewFolder={() => {
            setNewFolderName("");
            setModal({ type: "newFolder" });
          }}
          onBulkDelete={() =>
            setModal({ type: "bulkDelete", paths: Array.from(selectedItems) })
          }
          onBulkDownload={handleBulkDownload}
          onClearSelection={() => setSelectedItems(new Set())}
        />
      ) : (
        <FinderToolbar
          currentPath={currentPath}
          searchQuery={searchQuery}
          selectedCount={selectedItems.size}
          canWrite={permissions.canWrite}
          canAdmin={permissions.canAdmin}
          onNavigate={navigate}
          onRefresh={refreshList}
          onSearchChange={setSearchQuery}
          onNewFolder={() => {
            setNewFolderName("");
            setModal({ type: "newFolder" });
          }}
          onBulkDelete={() =>
            setModal({ type: "bulkDelete", paths: Array.from(selectedItems) })
          }
          onBulkDownload={handleBulkDownload}
          onClearSelection={() => setSelectedItems(new Set())}
        />
      )}

      {error && (
        <ErrorAlert message={error} onDismiss={() => setError(null)} />
      )}

      <div className="flex-1 min-h-0 flex overflow-hidden">
        {/* File list */}
        <div
          className={`${
            viewer.mode !== "closed"
              ? "w-[320px] shrink-0 border-r border-pir"
              : "flex-1"
          } overflow-y-auto`}
        >
          <FileList
            items={filteredItems}
            isLoading={isLoading}
            selectedItem={selectedItem}
            selectedItems={selectedItems}
            highlightedItem={highlightedItem}
            onSelect={setSelectedItem}
            onMultiSelect={handleMultiSelect}
            onOpen={handleOpen}
            onDownload={handleDownloadItem}
            onRename={permissions.canWrite ? (item) => setModal({ type: "rename", item }) : () => {}}
            onDelete={permissions.canAdmin ? (item) => setModal({ type: "delete", item }) : () => {}}
            onMove={permissions.canWrite ? (item) => setModal({ type: "move", item }) : () => {}}
            onContextMenu={handleListContextMenu}
            compact={viewer.mode !== "closed"}
          />
        </div>

        {/* Empty viewer welcome state — only in v2 and when nothing is open */}
        {v2 && viewer.mode === "closed" && filteredItems.length > 0 && (
          <div className="flex-1 min-w-0 overflow-hidden border-l border-pir">
            <FinderEmptyViewer onOpenSearch={() => {
              const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPod|iPad/.test(navigator.platform);
              window.dispatchEvent(new KeyboardEvent("keydown", {
                key: "k", code: "KeyK", metaKey: isMac, ctrlKey: !isMac, bubbles: true,
              }));
            }} />
          </div>
        )}

        {/* Viewer/Editor panel */}
        {viewer.mode !== "closed" && (
          <div className="flex-1 min-w-0 overflow-hidden">
            <FileViewer
              viewer={viewer}
              onSave={handleSave}
              onClose={handleCloseViewer}
              onDirtyChange={(dirty) => {
                if (viewer.mode === "editing") {
                  setViewer({ ...viewer, dirty });
                }
              }}
            />
          </div>
        )}
      </div>

      {/* Context menu (right-click list row) */}
      {contextMenu && (
        <FinderContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          items={contextMenu.items}
          onClose={() => setContextMenu(null)}
        />
      )}

      {/* Modals */}
      {modal.type === "rename" && (
        <RenameModal
          path={modal.item.path}
          currentName={modal.item.name}
          onClose={() => setModal({ type: "none" })}
          onRenamed={refreshList}
        />
      )}

      {modal.type === "move" && (
        <MoveModal
          sourcePath={modal.item.path}
          sourceName={modal.item.name}
          onClose={() => setModal({ type: "none" })}
          onMoved={refreshList}
        />
      )}

      {modal.type === "delete" && (
        <ConfirmModal
          title="Delete"
          message={`Delete "${modal.item.name}"? This cannot be undone.`}
          confirmLabel="Delete"
          danger
          onConfirm={handleDeleteConfirm}
          onCancel={() => setModal({ type: "none" })}
        />
      )}

      {modal.type === "bulkDelete" && (
        <ConfirmModal
          title="Delete selected"
          message={`Delete ${modal.paths.length} items? This cannot be undone.`}
          confirmLabel="Delete all"
          danger
          onConfirm={handleDeleteConfirm}
          onCancel={() => setModal({ type: "none" })}
        />
      )}

      {modal.type === "newFolder" && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleNewFolder();
            }}
            className="bg-pir-surface-0 border border-pir rounded-lg w-[360px]"
          >
            <div className="px-4 py-3 border-b border-pir">
              <h3 className="text-label text-pir-text-primary">New Folder</h3>
            </div>
            <div className="p-4">
              <input
                autoFocus
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                className="w-full px-3 py-2 bg-pir-surface-1 border border-pir rounded text-caption text-pir-text-primary focus:outline-none focus:border-pir-accent"
                placeholder="Folder name"
              />
            </div>
            <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-pir">
              <button
                type="button"
                onClick={() => setModal({ type: "none" })}
                className="px-3 py-1.5 text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!newFolderName.trim()}
                className="px-3 py-1.5 text-caption bg-pir-accent text-white rounded hover:bg-pir-accent/90 disabled:opacity-50 transition-colors"
              >
                Create
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------------
// v2 inline toolbar — lives ABOVE the list (subbar already shows breadcrumb +
// upload + search). Keeps filter input, refresh, new-folder, and bulk actions.
// -----------------------------------------------------------------------------

interface FinderV2ActionsBarProps {
  searchQuery: string;
  onSearchChange: (q: string) => void;
  selectedCount: number;
  canWrite: boolean;
  canAdmin: boolean;
  onRefresh: () => void;
  onNewFolder: () => void;
  onBulkDelete: () => void;
  onBulkDownload: () => void;
  onClearSelection: () => void;
}

function FinderV2ActionsBar({
  searchQuery,
  onSearchChange,
  selectedCount,
  canWrite,
  canAdmin,
  onRefresh,
  onNewFolder,
  onBulkDelete,
  onBulkDownload,
  onClearSelection,
}: FinderV2ActionsBarProps) {
  return (
    <div className="bg-pir-surface-0 border-b border-pir shrink-0 flex flex-col">
      <div className="flex items-center gap-2 px-3 py-2">
        <div className="relative flex-1 max-w-[320px]">
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden
            className="absolute text-pir-text-muted pointer-events-none"
            style={{ left: 6, top: "50%", transform: "translateY(-50%)" }}
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Filter current folder…"
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
        <div className="flex items-center gap-1 ml-auto shrink-0">
          {canWrite && (
            <button
              type="button"
              onClick={onNewFolder}
              title="New folder"
              aria-label="New folder"
              className="p-1.5 text-pir-text-tertiary hover:text-pir-accent transition-colors"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
                <path d="M3.75 3A1.75 1.75 0 002 4.75v3.26a3.235 3.235 0 011.75-.51h12.5c.644 0 1.245.188 1.75.51V6.75A1.75 1.75 0 0016.25 5h-4.836a.25.25 0 01-.177-.073L9.823 3.513A1.75 1.75 0 008.586 3H3.75z" />
                <path d="M3.75 9A1.75 1.75 0 002 10.75v4.5c0 .966.784 1.75 1.75 1.75h12.5A1.75 1.75 0 0018 15.25v-4.5A1.75 1.75 0 0016.25 9H3.75z" />
              </svg>
            </button>
          )}
          <button
            type="button"
            onClick={onRefresh}
            title="Refresh"
            aria-label="Refresh listing"
            className="p-1.5 text-pir-text-tertiary hover:text-pir-accent transition-colors"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
              <path
                fillRule="evenodd"
                d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H4.28a.75.75 0 00-.75.75v3.955a.75.75 0 001.5 0v-2.134l.235.234A7 7 0 0017.25 10a.75.75 0 00-1.5 0 5.5 5.5 0 01-.438 2.424zM4.688 8.576a5.5 5.5 0 019.201-2.466l.312.311h-2.433a.75.75 0 000 1.5h3.952a.75.75 0 00.75-.75V3.216a.75.75 0 00-1.5 0v2.134l-.235-.234A7 7 0 002.75 10a.75.75 0 001.5 0 5.5 5.5 0 01.438-2.424z"
                clipRule="evenodd"
              />
            </svg>
          </button>
        </div>
      </div>
      {selectedCount > 0 && (
        <div
          className="flex items-center gap-2 px-3 py-1.5 border-t border-pir"
          style={{ background: "hsl(var(--pir-accent) / 0.06)" }}
        >
          <span
            className="text-pir-accent uppercase"
            style={{
              fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
              fontSize: 10,
              letterSpacing: "0.14em",
              fontWeight: 600,
            }}
          >
            {selectedCount} selected
          </span>
          <button
            type="button"
            onClick={onBulkDownload}
            className="text-caption text-pir-text-secondary hover:text-pir-text-primary transition-colors"
          >
            Download
          </button>
          {canAdmin && (
            <button
              type="button"
              onClick={onBulkDelete}
              className="text-caption text-rose-400 hover:text-rose-300 transition-colors"
            >
              Delete
            </button>
          )}
          <button
            type="button"
            onClick={onClearSelection}
            className="ml-auto text-caption text-pir-text-muted hover:text-pir-text-secondary transition-colors"
          >
            Clear
          </button>
        </div>
      )}
    </div>
  );
}
