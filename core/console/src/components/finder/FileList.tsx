"use client";

import type { FinderListItem } from "@/lib/types";
import FileListItem from "./FileListItem";

interface FileListProps {
  items: FinderListItem[];
  isLoading: boolean;
  selectedItem: string | null;
  highlightedItem?: string | null;
  onSelect: (path: string | null) => void;
  onOpen: (item: FinderListItem) => void;
  onDownload: (item: FinderListItem) => void;
  compact?: boolean;
}

export default function FileList({
  items,
  isLoading,
  selectedItem,
  highlightedItem = null,
  onSelect,
  onOpen,
  onDownload,
  compact = false,
}: FileListProps) {
  if (isLoading) {
    return (
      <div className="space-y-2 p-4">
        {Array.from({ length: 8 }).map((_, index) => (
          <div key={index} className="h-8 animate-pulse rounded bg-pir-surface-1" />
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-caption text-pir-text-muted">
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
          highlighted={highlightedItem === item.path}
          compact={compact}
          onClick={() => onSelect(item.path)}
          onDoubleClick={() => onOpen(item)}
          onDownload={() => onDownload(item)}
        />
      ))}
    </div>
  );
}
