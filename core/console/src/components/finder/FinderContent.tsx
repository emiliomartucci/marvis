"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { finderDownload, getFinderFile, getFinderList } from "@/lib/api";
import type { FinderFileContent, FinderListItem } from "@/lib/types";
import { ErrorAlert } from "@/components/ui/ErrorAlert";
import FileList from "./FileList";
import FileViewer from "./FileViewer";
import FinderEmptyViewer from "./FinderEmptyViewer";

interface FinderContentProps {
  v2?: boolean;
  onOpenedFileChange?: (file: FinderFileContent | null) => void;
  onListInvalidate?: () => void;
}

export default function FinderContent({
  v2 = false,
  onOpenedFileChange,
  onListInvalidate,
}: FinderContentProps = {}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const currentPath = searchParams.get("path") || "";
  const highlightPath = searchParams.get("highlight");
  const [items, setItems] = useState<FinderListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [openedFile, setOpenedFile] = useState<FinderFileContent | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  const loadList = useCallback(() => {
    setLoading(true);
    setError(null);
    getFinderList(currentPath)
      .then((result) => {
        setItems(result.items);
        onListInvalidate?.();
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load directory"))
      .finally(() => setLoading(false));
  }, [currentPath, onListInvalidate]);

  useEffect(() => {
    setOpenedFile(null);
    onOpenedFileChange?.(null);
    setSelectedPath(null);
    setSearchQuery("");
    loadList();
  }, [currentPath, loadList, onOpenedFileChange]);

  const filteredItems = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return query ? items.filter((item) => item.name.toLowerCase().includes(query)) : items;
  }, [items, searchQuery]);

  const navigate = useCallback((path: string) => {
    router.push(path ? `/finder/?path=${encodeURIComponent(path)}` : "/finder/");
  }, [router]);

  const openItem = useCallback(async (item: FinderListItem) => {
    if (item.is_dir) {
      navigate(item.path);
      return;
    }
    try {
      const file = await getFinderFile(item.path);
      setOpenedFile(file);
      onOpenedFileChange?.(file);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to open file");
    }
  }, [navigate, onOpenedFileChange]);

  const downloadItem = useCallback(async (item: FinderListItem) => {
    try {
      const blob = await finderDownload(item.path);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = item.name;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch {
      setError("Download failed");
    }
  }, []);

  const parentPath = currentPath.split("/").filter(Boolean).slice(0, -1).join("/");

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {!v2 && (
        <header className="flex shrink-0 items-center gap-2 border-b border-pir bg-pir-surface-0 px-3 py-2">
          <button type="button" onClick={() => navigate(parentPath)} disabled={!currentPath} className="rounded px-2 py-1 text-caption text-pir-text-muted disabled:opacity-40">
            Back
          </button>
          <span className="min-w-0 flex-1 truncate font-mono text-caption text-pir-text-secondary">{currentPath || "~"}</span>
          <button type="button" onClick={loadList} className="rounded px-2 py-1 text-caption text-pir-text-muted hover:text-pir-text-secondary">
            Refresh
          </button>
          <input
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            placeholder="Filter files"
            aria-label="Filter files"
            className="w-44 rounded border border-pir bg-pir-base px-2 py-1 text-caption"
          />
        </header>
      )}

      {error && <ErrorAlert message={error} onDismiss={() => setError(null)} />}

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <div className={openedFile ? "w-[360px] shrink-0 overflow-y-auto border-r border-pir" : "flex-1 overflow-y-auto"}>
          <FileList
            items={filteredItems}
            isLoading={loading}
            selectedItem={selectedPath}
            highlightedItem={highlightPath}
            onSelect={setSelectedPath}
            onOpen={openItem}
            onDownload={downloadItem}
            compact={Boolean(openedFile)}
          />
        </div>
        {openedFile ? (
          <div className="min-w-0 flex-1 overflow-hidden">
            <FileViewer
              file={openedFile}
              onClose={() => {
                setOpenedFile(null);
                onOpenedFileChange?.(null);
              }}
            />
          </div>
        ) : v2 && filteredItems.length > 0 ? (
          <div className="min-w-0 flex-1 overflow-hidden border-l border-pir">
            <FinderEmptyViewer onOpenSearch={() => {
              const isMac = /Mac|iPhone|iPod|iPad/.test(navigator.platform);
              window.dispatchEvent(new KeyboardEvent("keydown", {
                key: "k",
                code: "KeyK",
                metaKey: isMac,
                ctrlKey: !isMac,
                bubbles: true,
              }));
            }} />
          </div>
        ) : null}
      </div>
    </div>
  );
}
