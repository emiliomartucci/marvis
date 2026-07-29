// v1.0.0 - 2026-04-22 - Finder v2 layout: subbar + sidebar + inline viewer (PR #10)
"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getFinderList } from "@/lib/api";
import type { FinderFileContent } from "@/lib/types";
import { useAuth } from "@/lib/auth";
import FinderSidebarV2 from "./FinderSidebarV2";
import FinderContent from "./FinderContent";
import FinderSubbar from "./FinderSubbar";
import UploadModal from "./UploadModal";

/**
 * Signal used to focus the global ⌘K palette from the Finder subbar trigger.
 * GlobalSearch already handles the Cmd/Ctrl+K keydown, so we simulate that
 * event rather than coupling the two components directly.
 */
function openGlobalSearch() {
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPod|iPad/.test(navigator.platform);
  const ev = new KeyboardEvent("keydown", {
    key: "k",
    code: "KeyK",
    metaKey: isMac,
    ctrlKey: !isMac,
    bubbles: true,
  });
  window.dispatchEvent(ev);
}

export default function FinderV2() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const currentPath = searchParams.get("path") || "";
  const { permissions } = useAuth();

  const [openedFile, setOpenedFile] = useState<FinderFileContent | null>(null);
  const [folderCount, setFolderCount] = useState(0);
  const [fileCount, setFileCount] = useState(0);
  const [showUpload, setShowUpload] = useState(false);
  const [listReloadKey, setListReloadKey] = useState(0);

  // Fetch counts for the current folder (for the eyebrow "N folders / M files")
  useEffect(() => {
    let cancelled = false;
    getFinderList(currentPath)
      .then((res) => {
        if (cancelled) return;
        const folders = res.items.filter((i) => i.is_dir).length;
        const files = res.items.length - folders;
        setFolderCount(folders);
        setFileCount(files);
      })
      .catch(() => {
        if (!cancelled) {
          setFolderCount(0);
          setFileCount(0);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [currentPath, listReloadKey]);

  const handleNavigate = useCallback(
    (path: string) => {
      router.push(path ? `/finder/?path=${encodeURIComponent(path)}` : "/finder/");
    },
    [router]
  );

  return (
    <div className="flex flex-col h-full">
      <FinderSubbar
        currentPath={currentPath}
        folderCount={folderCount}
        fileCount={fileCount}
        openedFile={openedFile}
        canWrite={permissions.canWrite}
        onNavigate={handleNavigate}
        onUpload={() => setShowUpload(true)}
        onOpenSearch={openGlobalSearch}
      />
      <div className="flex flex-1 min-h-0">
        <FinderSidebarV2 />
        <div className="flex-1 overflow-hidden">
          <Suspense fallback={null}>
            <FinderContent
              onOpenedFileChange={setOpenedFile}
              onListInvalidate={() => setListReloadKey((k) => k + 1)}
              v2
            />
          </Suspense>
        </div>
      </div>
      {showUpload && (
        <UploadModal
          currentPath={currentPath}
          onClose={() => setShowUpload(false)}
          onUploaded={() => setListReloadKey((k) => k + 1)}
        />
      )}
    </div>
  );
}
