// v1.2.0 - 2026-04-27 - PR #21: search bar semantic → searchMatches Set<slug> highlight project + edge dim
// v1.1.0 - 2026-04-24 - PR #3: swap mock → fetch live /graph/cosmo via useCosmoData
// v1.0.0 - 2026-04-24 - Wrapper pagina canvas Cosmo.
//
// Stato selezione + filter toggle. Esc listener globale (M-FE-15).
// Wrap `GraphCanvas` in `CosmoCanvasErrorBoundary` — `GraphInspector` fuori
// boundary (M-FE-16: inspector sopravvive a canvas crash).
// Dati: fetch live da `/graph/cosmo` via `useCosmoData` (AbortController + 10s timeout).
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CosmoCanvasErrorBoundary } from "./CosmoCanvasErrorBoundary";
import { GraphCanvas, type SelectedDir } from "./GraphCanvas";
import { GraphInspector } from "./GraphInspector";
import { useCosmoData } from "./useCosmoData";
import { useGraphSearch } from "./useGraphSearch";

// -----------------------------------------------------------------------------
// States ancillari (skeleton + error fallback). Inline: sono piccoli e
// accoppiati a GraphPage → zero giustificazione per un file a parte.
// -----------------------------------------------------------------------------

const SHELL_STYLE: React.CSSProperties = {
  position: "relative",
  display: "flex",
  width: "100%",
  height: "100%",
  minHeight: 0,
  overflow: "hidden",
  background: "var(--pir-surface)",
  color: "var(--pir-text)",
  fontFamily: "var(--font-mono, ui-monospace, monospace)",
};

function CosmoSkeleton() {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label="Caricamento grafo"
      style={{
        ...SHELL_STYLE,
        alignItems: "center",
        justifyContent: "center",
        fontSize: 12,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        opacity: 0.7,
      }}
    >
      Loading cosmo...
    </div>
  );
}

function CosmoErrorFallback({
  error,
  onRetry,
}: {
  readonly error: string;
  readonly onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      style={{
        ...SHELL_STYLE,
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 16,
        padding: 24,
        textAlign: "center",
      }}
    >
      <div style={{ fontSize: 14, maxWidth: 480 }}>
        Impossibile caricare il grafo.
      </div>
      <div
        style={{
          fontSize: 12,
          opacity: 0.6,
          maxWidth: 640,
          wordBreak: "break-word",
        }}
      >
        {error}
      </div>
      <button
        type="button"
        onClick={onRetry}
        style={{
          background: "transparent",
          border: "1px solid currentColor",
          color: "inherit",
          padding: "8px 14px",
          fontSize: 12,
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          cursor: "pointer",
          fontFamily: "inherit",
        }}
      >
        Riprova
      </button>
    </div>
  );
}

// -----------------------------------------------------------------------------
// GraphPage
// -----------------------------------------------------------------------------

export function GraphPage() {
  const { data, error, loading } = useCosmoData();

  const [selected, setSelected] = useState<string | null>(null);
  const [selectedDir, setSelectedDir] = useState<SelectedDir | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [showLabels, setShowLabels] = useState(true);
  const [showSatellites, setShowSatellites] = useState(true);
  const [showEdges, setShowEdges] = useState(true);

  // Search bar semantic (PR #21). Empty/short query → searchMatches=null sotto.
  // Memo del Set evita re-creation tra render quando query e' empty.
  const [searchQuery, setSearchQuery] = useState("");
  const rawMatches = useGraphSearch(searchQuery);
  const hasActiveSearch = searchQuery.trim().length >= 2;
  const searchMatches = useMemo<ReadonlySet<string> | null>(
    () => (hasActiveSearch ? rawMatches : null),
    [hasActiveSearch, rawMatches],
  );

  // Esc listener globale: clear selection + dir + hover.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setSelected(null);
        setSelectedDir(null);
        setHovered(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const closeInspector = useCallback(() => {
    setSelected(null);
    setSelectedDir(null);
  }, []);

  const toggleLabels = useCallback(() => setShowLabels((v) => !v), []);
  const toggleSatellites = useCallback(() => setShowSatellites((v) => !v), []);
  const toggleEdges = useCallback(() => setShowEdges((v) => !v), []);

  const retry = useCallback(() => {
    // Ricarico la pagina per triggerare un nuovo effect del hook.
    // Alternative (setReqId) richiederebbero il hook a accettare un key —
    // non serve per un endpoint GET idempotente.
    if (typeof window !== "undefined") window.location.reload();
  }, []);

  if (loading) return <CosmoSkeleton />;
  if (error) return <CosmoErrorFallback error={error} onRetry={retry} />;
  if (!data) return <CosmoSkeleton />; // edge case: no-data no-error no-loading

  return (
    <div style={SHELL_STYLE}>
      <CosmoCanvasErrorBoundary>
        <GraphCanvas
          projects={data.projects}
          edges={data.edges}
          selected={selected}
          hovered={hovered}
          selectedDir={selectedDir}
          showLabels={showLabels}
          showSatellites={showSatellites}
          showEdges={showEdges}
          searchQuery={searchQuery}
          searchMatches={searchMatches}
          onSelect={setSelected}
          onHover={setHovered}
          onSelectDir={setSelectedDir}
          onToggleLabels={toggleLabels}
          onToggleSatellites={toggleSatellites}
          onToggleEdges={toggleEdges}
          onSearchQueryChange={setSearchQuery}
        />
      </CosmoCanvasErrorBoundary>
      {(selected || selectedDir) && (
        <GraphInspector
          selected={selected}
          selectedDir={selectedDir}
          onClose={closeInspector}
        />
      )}
    </div>
  );
}
