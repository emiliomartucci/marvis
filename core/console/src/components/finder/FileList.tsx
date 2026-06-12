"use client";

import type { FinderListItem } from "@/lib/types";
import FileListItem from "./FileListItem";

interface FileListProps {
  items: FinderListItem[];
  isLoading: boolean;
  selectedItem: string | null;
  selectedItems: Set<string>;
  highlightedItem?: string | null;
  onSelect: (path: string | null) => void;
  onMultiSelect: (path: string, e: React.MouseEvent) => void;
  onOpen: (item: FinderListItem) => void;
  onDownload: (item: FinderListItem) => void;
  onRename: (item: FinderListItem) => void;
  onDelete: (item: FinderListItem) => void;
  onMove: (item: FinderListItem) => void;
  onContextMenu?: (e: React.MouseEvent, item: FinderListItem) => void;
  compact?: boolean;
}

export default function FileList({
  items,
  isLoading,
  selectedItem,
  selectedItems,
  highlightedItem = null,
  onSelect,
  onMultiSelect,
  onOpen,
  onDownload,
  onRename,
  onDelete,
  onMove,
  onContextMenu,
  compact = false,
}: FileListProps) {
  if (isLoading) {
    return (
      <div className="p-4 space-y-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="h-8 bg-pir-surface-1 rounded animate-pulse"
          />
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-caption text-pir-text-muted">
        Empty directory
      </div>
    );
  }

  return (
    <div className="divide-y divide-pir">
      {items.map((item) => (
        <FileListItem
          key={item.path}
          item={item}
          selected={selectedItem === item.path}
          multiSelected={selectedItems.has(item.path)}
          highlighted={highlightedItem === item.path}
          compact={compact}
          onClick={(e) => {
            if (e.ctrlKey || e.metaKey || e.shiftKey) {
              onMultiSelect(item.path, e);
            } else {
              onSelect(item.path);
            }
          }}
          onDoubleClick={() => onOpen(item)}
          onDownload={() => onDownload(item)}
          onRename={() => onRename(item)}
          onDelete={() => onDelete(item)}
          onMove={() => onMove(item)}
          onContextMenu={onContextMenu}
        />
      ))}
    </div>
  );
}
