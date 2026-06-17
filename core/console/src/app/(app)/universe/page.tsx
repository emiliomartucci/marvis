"use client";

import { GraphLensShell } from "@/components/graph/GraphLensShell";
import { UNIVERSE_ROUTE_LENSES } from "@/components/graph/LensSwitcher";

export default function UniversePage() {
  return (
    <div className="flex min-h-0 flex-1 bg-pir-base">
      <GraphLensShell
        lenses={UNIVERSE_ROUTE_LENSES}
        defaultLens="cosmo"
        basePath="/universe"
      />
    </div>
  );
}
