"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getFinderList } from "@/lib/api";
import type { FinderFileContent } from "@/lib/types";
import FinderSidebarV2 from "./FinderSidebarV2";
import FinderContent from "./FinderContent";
import FinderSubbar from "./FinderSubbar";

function openGlobalSearch() {
  const isMac = /Mac|iPhone|iPod|iPad/.test(navigator.platform);
  window.dispatchEvent(new KeyboardEvent("keydown", {
    key: "k",
    code: "KeyK",
    metaKey: isMac,
    ctrlKey: !isMac,
    bubbles: true,
  }));
}

export default function FinderV2() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const currentPath = searchParams.get("path") || "";
  const [openedFile, setOpenedFile] = useState<FinderFileContent | null>(null);
  const [folderCount, setFolderCount] = useState(0);
  const [fileCount, setFileCount] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    getFinderList(currentPath)
      .then((result) => {
        if (cancelled) return;
        const folders = result.items.filter((item) => item.is_dir).length;
        setFolderCount(folders);
        setFileCount(result.items.length - folders);
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
  }, [currentPath, reloadKey]);

  const navigate = useCallback((path: string) => {
    router.push(path ? `/finder/?path=${encodeURIComponent(path)}` : "/finder/");
  }, [router]);
  const invalidateList = useCallback(() => setReloadKey((value) => value + 1), []);

  return (
    <div className="flex h-full flex-col">
      <FinderSubbar
        currentPath={currentPath}
        folderCount={folderCount}
        fileCount={fileCount}
        openedFile={openedFile}
        onNavigate={navigate}
        onOpenSearch={openGlobalSearch}
      />
      <div className="flex min-h-0 flex-1">
        <FinderSidebarV2 />
        <div className="min-w-0 flex-1 overflow-hidden">
          <Suspense fallback={null}>
            <FinderContent
              v2
              onOpenedFileChange={setOpenedFile}
              onListInvalidate={invalidateList}
            />
          </Suspense>
        </div>
      </div>
    </div>
  );
}
