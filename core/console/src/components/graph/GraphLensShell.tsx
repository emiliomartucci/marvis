"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, type CSSProperties } from "react";

import { useT } from "@/lib/i18n";
import {
  GRAPH_LENSES,
  LensSwitcher,
  type LensId,
  type LensOption,
} from "@/components/graph/LensSwitcher";
import { CodexLens } from "@/components/graph/pr-impact";
import { GraphPage as CosmoGraphPage } from "@/components/graph/cosmo/GraphPage";

function useApplyThemeV2() {
  useEffect(() => {
    const html = document.documentElement;
    const wasAlreadyV2 = html.classList.contains("theme-v2");
    if (!wasAlreadyV2) html.classList.add("theme-v2");
    return () => {
      if (!wasAlreadyV2) html.classList.remove("theme-v2");
    };
  }, []);
}

function parseLens(
  raw: string | null,
  lenses: readonly LensOption[],
  defaultLens: LensId,
): LensId {
  if (raw && lenses.some((lens) => lens.id === raw)) {
    return raw as LensId;
  }
  return defaultLens;
}

export function GraphLensShell({
  lenses = GRAPH_LENSES,
  defaultLens = "cosmo",
  basePath = "/graph",
}: {
  readonly lenses?: readonly LensOption[];
  readonly defaultLens?: LensId;
  readonly basePath?: string;
}) {
  useApplyThemeV2();
  return (
    <Suspense fallback={<LensShellSkeleton />}>
      <GraphShellContent lenses={lenses} defaultLens={defaultLens} basePath={basePath} />
    </Suspense>
  );
}

function GraphShellContent({
  lenses,
  defaultLens,
  basePath,
}: {
  readonly lenses: readonly LensOption[];
  readonly defaultLens: LensId;
  readonly basePath: string;
}) {
  const params = useSearchParams();
  const lens = parseLens(params?.get("lens") ?? null, lenses, defaultLens);

  return (
    <div data-tour={basePath === "/universe" ? "universe" : undefined} style={SHELL_STYLE}>
      {lens !== "codex" && (
        <div style={SWITCHER_OVERLAY}>
          <LensSwitcher active={lens} lenses={lenses} basePath={basePath} />
        </div>
      )}

      {lens === "cosmo" && <CosmoGraphPage />}
      {lens === "codex" && <CodexLens />}
    </div>
  );
}

function LensShellSkeleton() {
  const { t } = useT();
  return (
    <div style={{ ...SHELL_STYLE, alignItems: "center", justifyContent: "center" }}>
      <p style={{ color: "var(--pir-text-tertiary)", fontSize: 12 }}>{t.graph.loading}</p>
    </div>
  );
}

const SHELL_STYLE: CSSProperties = {
  position: "relative",
  display: "flex",
  width: "100%",
  height: "100%",
  minHeight: 0,
  overflow: "hidden",
  background: "hsl(var(--pir-base))",
  color: "var(--pir-text-primary)",
  fontFamily: "var(--pir-font-sans, 'IBM Plex Sans', sans-serif)",
};

const SWITCHER_OVERLAY: CSSProperties = {
  position: "absolute",
  top: 12,
  left: 12,
  zIndex: 10,
  pointerEvents: "auto",
};
