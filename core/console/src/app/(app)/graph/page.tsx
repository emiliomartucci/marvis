// v4.0.0 - 2026-05-17 - Lens switch: /graph?lens=cosmo (default) | codex | universe
// Renders ONE canvas based on `?lens=` query param + a persistent LensSwitcher.
"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, type CSSProperties } from "react";

import { CodexLens } from "@/components/graph/pr-impact";
import { LensSwitcher, type LensId } from "@/components/graph/LensSwitcher";
import { GraphPage as CosmoGraphPage } from "@/components/graph/cosmo/GraphPage";

/**
 * Apply `.theme-v2` (TE industrial palette) for the duration of `/graph`,
 * unchanged from v3. The theme-v2 class scopes the --pir-* tokens to the
 * design-system canonical values without forcing light/dark.
 */
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

const VALID_LENSES: ReadonlyArray<LensId> = ["universe", "cosmo", "codex"];

function parseLens(raw: string | null): LensId {
  if (raw && (VALID_LENSES as ReadonlyArray<string>).includes(raw)) {
    return raw as LensId;
  }
  return "cosmo";
}

export default function Page() {
  useApplyThemeV2();
  return (
    <Suspense fallback={<LensShellSkeleton />}>
      <GraphShell />
    </Suspense>
  );
}

function GraphShell() {
  const params = useSearchParams();
  const lens = parseLens(params?.get("lens") ?? null);

  // Codex integra LensSwitcher nel CodexHeader (in flow). Cosmo/Universe
  // usano l'overlay floating default.
  return (
    <div style={SHELL_STYLE}>
      {lens !== "codex" && (
        <div style={SWITCHER_OVERLAY}>
          <LensSwitcher active={lens} />
        </div>
      )}

      {lens === "cosmo" && <CosmoGraphPage />}
      {lens === "codex" && <CodexLens />}
      {lens === "universe" && <UniverseComingSoon />}
    </div>
  );
}

function UniverseComingSoon() {
  return (
    <div style={UNIVERSE_STYLE}>
      <h2 style={UNIVERSE_TITLE}>Universe</h2>
      <p style={UNIVERSE_BODY}>
        Drilling out beyond Cosmo — vista cross-project con ondate temporali.
      </p>
      <p style={UNIVERSE_HINT}>Disponibile in v1.1. Per ora usa Cosmo o Codex.</p>
    </div>
  );
}

function LensShellSkeleton() {
  return (
    <div style={{ ...SHELL_STYLE, alignItems: "center", justifyContent: "center" }}>
      <p style={{ color: "var(--pir-text-tertiary)", fontSize: 12 }}>Caricamento…</p>
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

const UNIVERSE_STYLE: CSSProperties = {
  flex: 1,
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  gap: 8,
  padding: 24,
  textAlign: "center",
};

const UNIVERSE_TITLE: CSSProperties = {
  fontFamily: "var(--pir-font-mono, monospace)",
  fontSize: 14,
  fontWeight: 700,
  letterSpacing: "0.2em",
  textTransform: "uppercase",
  color: "hsl(var(--pir-accent))",
  margin: 0,
};

const UNIVERSE_BODY: CSSProperties = {
  fontSize: 14,
  color: "var(--pir-text-primary)",
  marginTop: 8,
};

const UNIVERSE_HINT: CSSProperties = {
  fontSize: 12,
  color: "var(--pir-text-tertiary)",
};
