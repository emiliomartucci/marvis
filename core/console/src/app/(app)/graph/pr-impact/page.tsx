// Query-param compatible static export route: /graph/pr-impact?prId=<id>
"use client";

import { Suspense } from "react";

import { CodexLens } from "@/components/graph/pr-impact";

export default function Page() {
  return (
    <Suspense fallback={<PrImpactSkeleton />}>
      <CodexLens />
    </Suspense>
  );
}

function PrImpactSkeleton() {
  return (
    <div className="flex flex-1 items-center justify-center bg-pir-base text-caption text-pir-text-tertiary">
      Loading...
    </div>
  );
}
