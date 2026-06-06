// v1.1.0 - 2026-04-22 - Gate theme-v2 variant with Subbar + inline viewer (PR #10)
"use client";

import { Suspense } from "react";
import FinderSidebar from "@/components/finder/FinderSidebar";
import FinderContent from "@/components/finder/FinderContent";
import FinderV2 from "@/components/finder/FinderV2";
import { useDesignV2 } from "@/lib/useDesignV2";

export default function FinderPage() {
  const v2 = useDesignV2();

  if (v2) {
    return (
      <Suspense fallback={null}>
        <FinderV2 />
      </Suspense>
    );
  }

  // v1 legacy layout — intentionally untouched to guarantee zero regression.
  return (
    <div className="flex flex-1 min-h-0 h-full">
      <aside className="hidden md:flex w-[240px] shrink-0 flex-col bg-pir-surface-0 border-r border-pir overflow-y-auto">
        <FinderSidebar />
      </aside>
      <div className="flex-1 overflow-hidden">
        <Suspense fallback={null}>
          <FinderContent />
        </Suspense>
      </div>
    </div>
  );
}
