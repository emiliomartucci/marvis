// v1.2.0 - 2026-04-27 - PR #21: HudLegend verticale collassabile (header click toggle,
//                       chips stacked column, persistenza expanded in localStorage)
//                       + nuovo HudSearch (input semantic con icona lente, top-right).
// v1.1.0 - 2026-04-24 - stopPointerDown su ogni root HUD: evita al container
//                       di entrare in pan-mode quando si clicca un button HUD
//                       (Beautify + Fit regredivano dopo PR #4 Cosmo Fidelity).
// v1.0.0 - 2026-04-24 - HUD consolidato canvas Cosmo (5 quadranti memoized).
//
// Tutti componenti memo con comparator custom basato su props semantici,
// non su zoom/pan live (M-FE-02 piano). MiniTog inline in HudFilters
// (H-03 piano). Nessun hex hardcoded — solo `hsl(var(--pir-*))` + `--bone-*`.
"use client";

import { memo, useCallback, useState, type CSSProperties, type ChangeEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { BeautifyMenu } from "./BeautifyMenu";
import { COSMO_KIND_TO_DOC_KIND, KIND_LABELS } from "./kindLabels";
import { docTagColor } from "@/lib/docTags";
import type { BeautifyKind, Kind } from "./types";

const LEGEND_EXPANDED_LS_KEY = "marvisx.graph.legend.expanded";

// -----------------------------------------------------------------------------
// Styles condivisi HUD
// -----------------------------------------------------------------------------

const overlayBase: CSSProperties = {
  position: "absolute",
  padding: "8px 12px",
  background: "hsl(var(--pir-surface-0))",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  fontFamily: "var(--pir-font-sans)",
  fontSize: 11,
  color: "var(--pir-text-secondary)",
  pointerEvents: "auto",
};

const monoLabel: CSSProperties = {
  fontFamily: "var(--pir-font-mono)",
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
  color: "var(--pir-text-tertiary)",
};

/**
 * Blocca la propagazione del pointerdown verso il container canvas. Il
 * container usa `setPointerCapture` per abilitare il pan: se il capture parte
 * da un click su un button HUD, il button non riceve il `pointerup` successivo
 * e il `click` non viene mai dispatchato. Fermiamo il pointerdown alla root
 * HUD prima che il container possa catturare il pointer.
 */
function stopPointerDown(e: ReactPointerEvent<HTMLDivElement>): void {
  e.stopPropagation();
}

// -----------------------------------------------------------------------------
// HudBreadcrumb — top-left
// -----------------------------------------------------------------------------

/** @lintignore — props per Hud component consumata solo internamente da GraphCanvas. */
export interface HudBreadcrumbProps {
  selected: string | null;
  selectedDirName: string | null;
  nodeCount: number;
  edgeCount: number;
  onResetToUniverse: () => void;
  onResetToProject: () => void;
}

function HudBreadcrumbImpl({
  selected,
  selectedDirName,
  nodeCount,
  edgeCount,
  onResetToUniverse,
  onResetToProject,
}: HudBreadcrumbProps) {
  const style: CSSProperties = {
    ...overlayBase,
    left: 16,
    top: 16,
    display: "flex",
    gap: 8,
    alignItems: "center",
    ...monoLabel,
  };
  return (
    <div style={style} onPointerDown={stopPointerDown}>
      <span
        onClick={onResetToUniverse}
        style={{
          cursor: "pointer",
          color: selected ? "var(--pir-text-tertiary)" : "var(--pir-text-primary)",
        }}
      >
        UNIVERSE
      </span>
      {!selected && (
        <>
          <span style={{ opacity: 0.5 }}>·</span>
          <span>
            <span style={{ color: "var(--pir-text-primary)" }}>{nodeCount}</span> NODES
          </span>
          <span style={{ opacity: 0.5 }}>·</span>
          <span>
            <span style={{ color: "var(--pir-text-primary)" }}>{edgeCount}</span> EDGES
          </span>
        </>
      )}
      {selected && (
        <>
          <span style={{ opacity: 0.4 }}>›</span>
          <span
            onClick={onResetToProject}
            style={{
              cursor: "pointer",
              color: selectedDirName ? "var(--pir-text-tertiary)" : "hsl(var(--pir-accent))",
            }}
          >
            {selected}
          </span>
        </>
      )}
      {selectedDirName && (
        <>
          <span style={{ opacity: 0.4 }}>›</span>
          <span
            style={{
              color: "hsl(var(--pir-accent))",
              textTransform: "none",
              letterSpacing: "0.02em",
              fontWeight: 500,
            }}
          >
            {selectedDirName}
          </span>
        </>
      )}
    </div>
  );
}

export const HudBreadcrumb = memo(HudBreadcrumbImpl);

// -----------------------------------------------------------------------------
// HudFilters — top-right (3 toggle: labels / satellites / edges)
// -----------------------------------------------------------------------------

/** Inline mini toggle (reference MiniTog). */
function MiniTog({
  children,
  active,
  onClick,
}: {
  children: ReactNode;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <span
      onClick={onClick}
      role="button"
      style={{
        padding: "2px 6px",
        background: active ? "hsl(var(--pir-accent) / 0.18)" : "transparent",
        border: `1px solid ${active ? "hsl(var(--pir-accent) / 0.5)" : "var(--pir-border)"}`,
        borderRadius: 2,
        color: active ? "hsl(var(--pir-accent))" : "var(--pir-text-tertiary)",
        fontFamily: "var(--pir-font-mono)",
        fontSize: 9,
        letterSpacing: "0.08em",
        cursor: "pointer",
        userSelect: "none",
      }}
    >
      {children}
    </span>
  );
}

/** @lintignore — props per Hud component consumata solo internamente da GraphCanvas. */
export interface HudFiltersProps {
  showLabels: boolean;
  showSatellites: boolean;
  showEdges: boolean;
  onToggleLabels: () => void;
  onToggleSatellites: () => void;
  onToggleEdges: () => void;
}

function HudFiltersImpl({
  showLabels,
  showSatellites,
  showEdges,
  onToggleLabels,
  onToggleSatellites,
  onToggleEdges,
}: HudFiltersProps) {
  const style: CSSProperties = {
    ...overlayBase,
    right: 16,
    // top: 60 lascia spazio per HudSearch (height ~36px @ top: 16) sopra,
    // entrambi top-right ma stacked verticalmente.
    top: 60,
    padding: "10px 12px",
    display: "flex",
    flexDirection: "column",
    gap: 8,
    width: 220,
  };
  const rowStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    ...monoLabel,
  };
  return (
    <div style={style} onPointerDown={stopPointerDown}>
      <div style={rowStyle}>
        <span>Labels</span>
        <MiniTog active={showLabels} onClick={onToggleLabels}>
          {showLabels ? "ON" : "OFF"}
        </MiniTog>
      </div>
      <div style={rowStyle}>
        <span>Satellites</span>
        <MiniTog active={showSatellites} onClick={onToggleSatellites}>
          {showSatellites ? "ON" : "OFF"}
        </MiniTog>
      </div>
      <div style={rowStyle}>
        <span>Edges</span>
        <MiniTog active={showEdges} onClick={onToggleEdges}>
          {showEdges ? "ON" : "OFF"}
        </MiniTog>
      </div>
    </div>
  );
}

export const HudFilters = memo(HudFiltersImpl);

// -----------------------------------------------------------------------------
// HudLegend — bottom-left (kind palette + meta hints, verticale + collassabile)
// -----------------------------------------------------------------------------

/** Legge stato expanded da localStorage (default true). SSR-safe. */
function readLegendExpanded(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(LEGEND_EXPANDED_LS_KEY);
    if (raw === null) return true;
    return raw === "1";
  } catch {
    return true;
  }
}

/** @lintignore — props per Hud component consumata solo internamente da GraphCanvas. */
export interface HudLegendProps {
  kinds: readonly Kind[];
}

function HudLegendImpl({ kinds }: HudLegendProps) {
  const [expanded, setExpanded] = useState<boolean>(readLegendExpanded);

  const toggle = useCallback(() => {
    setExpanded((v) => {
      const next = !v;
      try {
        if (typeof window !== "undefined") {
          window.localStorage.setItem(LEGEND_EXPANDED_LS_KEY, next ? "1" : "0");
        }
      } catch {
        /* localStorage off — non-fatal */
      }
      return next;
    });
  }, []);

  const style: CSSProperties = {
    ...overlayBase,
    left: 16,
    bottom: 16,
    padding: expanded ? "10px 12px" : "6px 10px",
    display: "flex",
    flexDirection: "column",
    gap: 6,
    width: 140,
  };

  // Caret SVG ruotato a 0deg quando espanso (▾), 270deg quando collassato (▸).
  // CSS-only rotation via transform per evitare re-render costosi.
  const caretRotation = expanded ? "rotate(0deg)" : "rotate(-90deg)";

  return (
    <div style={style} onPointerDown={stopPointerDown}>
      <div
        onClick={toggle}
        role="button"
        aria-expanded={expanded}
        aria-label={expanded ? "Collapse legend" : "Expand legend"}
        style={{
          ...monoLabel,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          cursor: "pointer",
          userSelect: "none",
        }}
      >
        <span>Legend</span>
        <svg
          width="9"
          height="9"
          viewBox="0 0 9 9"
          style={{
            transform: caretRotation,
            transition: "transform 160ms",
            color: "var(--pir-text-tertiary)",
          }}
          aria-hidden="true"
        >
          <path d="M 1.5 3 L 4.5 6 L 7.5 3" stroke="currentColor" strokeWidth="1.2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>

      {expanded && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <svg width="14" height="14">
              <circle
                cx="7"
                cy="7"
                r="5"
                fill="none"
                stroke="hsl(var(--bone-300))"
                strokeWidth="1"
              />
            </svg>
            <span
              style={{
                fontFamily: "var(--pir-font-mono)",
                fontSize: 10,
                color: "var(--pir-text-secondary)",
              }}
            >
              PROJECT
            </span>
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 4,
              marginTop: 2,
            }}
          >
            {kinds.map((k) => {
              const activityKind = COSMO_KIND_TO_DOC_KIND[k];
              const c = docTagColor(activityKind);
              return (
                <span
                  key={k}
                  style={{
                    padding: "2px 6px",
                    background: c.bg,
                    color: c.fg,
                    border: "1px solid var(--pir-border)",
                    borderRadius: 2,
                    fontFamily: "var(--pir-font-mono)",
                    fontSize: 9,
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    textAlign: "center",
                  }}
                >
                  {KIND_LABELS[k]}
                </span>
              );
            })}
          </div>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 3,
              paddingTop: 4,
              marginTop: 2,
              borderTop: "1px solid var(--pir-border)",
              fontFamily: "var(--pir-font-mono)",
              fontSize: 10,
              color: "var(--pir-text-muted)",
            }}
          >
            <div>· size = degree</div>
            <div>· thickness = synapse</div>
            <div>· halo = pinned</div>
          </div>
        </>
      )}
    </div>
  );
}

export const HudLegend = memo(HudLegendImpl);

// -----------------------------------------------------------------------------
// HudSearch — top-right (input semantic, debounce orchestrated by useGraphSearch)
// -----------------------------------------------------------------------------

/** @lintignore — props HudSearch consumate solo internamente da GraphCanvas. */
export interface HudSearchProps {
  query: string;
  setQuery: (q: string) => void;
  isSearching?: boolean;
}

function HudSearchImpl({ query, setQuery, isSearching = false }: HudSearchProps) {
  const onChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setQuery(e.target.value),
    [setQuery],
  );
  const onClear = useCallback(() => setQuery(""), [setQuery]);

  const style: CSSProperties = {
    ...overlayBase,
    right: 16,
    top: 16,
    padding: "6px 10px",
    width: 200,
    display: "flex",
    alignItems: "center",
    gap: 6,
  };

  return (
    <div style={style} onPointerDown={stopPointerDown}>
      <svg
        width="11"
        height="11"
        viewBox="0 0 12 12"
        aria-hidden="true"
        style={{ flexShrink: 0, color: "var(--pir-text-tertiary)" }}
      >
        <circle cx="5" cy="5" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.1" />
        <line x1="7.5" y1="7.5" x2="10.5" y2="10.5" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
      </svg>
      <input
        type="text"
        value={query}
        onChange={onChange}
        placeholder="search projects..."
        aria-label="Search projects"
        style={{
          flex: 1,
          minWidth: 0,
          background: "transparent",
          border: "none",
          outline: "none",
          fontFamily: "var(--pir-font-mono)",
          fontSize: 11,
          color: "var(--pir-text-primary)",
          padding: 0,
        }}
      />
      {query.length > 0 && (
        <span
          onClick={onClear}
          role="button"
          aria-label="Clear search"
          style={{
            cursor: "pointer",
            color: "var(--pir-text-tertiary)",
            fontFamily: "var(--pir-font-mono)",
            fontSize: 11,
            lineHeight: 1,
            padding: "0 2px",
            opacity: isSearching ? 0.5 : 1,
          }}
        >
          ×
        </span>
      )}
    </div>
  );
}

export const HudSearch = memo(HudSearchImpl);

// -----------------------------------------------------------------------------
// HudZoom — bottom-center
// -----------------------------------------------------------------------------

/** @lintignore — props per Hud component consumata solo internamente da GraphCanvas. */
export interface HudZoomProps {
  zoom: number;
  onZoomOut: () => void;
  onZoomIn: () => void;
  onFit: () => void;
}

const zoomBtn: CSSProperties = {
  width: 22,
  height: 22,
  background: "transparent",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  color: "var(--pir-text-secondary)",
  fontFamily: "var(--pir-font-mono)",
  fontSize: 11,
  cursor: "pointer",
};

function HudZoomImpl({ zoom, onZoomOut, onZoomIn, onFit }: HudZoomProps) {
  const style: CSSProperties = {
    ...overlayBase,
    left: "50%",
    bottom: 16,
    transform: "translateX(-50%)",
    display: "flex",
    gap: 4,
    padding: "6px 8px",
    fontFamily: "var(--pir-font-mono)",
    fontSize: 10,
  };
  return (
    <div style={style} onPointerDown={stopPointerDown}>
      <button type="button" onClick={onZoomOut} style={zoomBtn} aria-label="zoom out">
        −
      </button>
      <span style={{ minWidth: 52, textAlign: "center", alignSelf: "center" }}>
        {Math.round(zoom * 100)}%
      </span>
      <button type="button" onClick={onZoomIn} style={zoomBtn} aria-label="zoom in">
        +
      </button>
      <span
        style={{
          width: 1,
          height: 14,
          background: "var(--pir-border)",
          margin: "0 2px",
          alignSelf: "center",
        }}
      />
      <button type="button" onClick={onFit} style={zoomBtn} aria-label="fit">
        fit
      </button>
    </div>
  );
}

// Comparator custom: riduciamo re-render durante zoom continuo confrontando a 2 decimali.
export const HudZoom = memo(HudZoomImpl, (a, b) => {
  return (
    Math.round(a.zoom * 100) === Math.round(b.zoom * 100) &&
    a.onZoomOut === b.onZoomOut &&
    a.onZoomIn === b.onZoomIn &&
    a.onFit === b.onFit
  );
});

// -----------------------------------------------------------------------------
// HudShortcuts — bottom-right (keyboard hints + BeautifyMenu host)
// -----------------------------------------------------------------------------

/** Inline shortcut label (reference Sc). */
function Sc({ k, children }: { k: string; children: ReactNode }) {
  return (
    <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
      <span
        style={{
          padding: "1px 4px",
          border: "1px solid var(--pir-border)",
          borderRadius: 2,
          color: "var(--pir-text-secondary)",
          minWidth: 10,
          textAlign: "center",
        }}
      >
        {k}
      </span>
      <span>{children}</span>
    </span>
  );
}

/** @lintignore — props per Hud component consumata solo internamente da GraphCanvas. */
export interface HudShortcutsProps {
  beautifyOpen: boolean;
  onToggleBeautify: () => void;
  onCloseBeautify: () => void;
  onBeautify: (kind: BeautifyKind) => void;
}

function HudShortcutsImpl({
  beautifyOpen,
  onToggleBeautify,
  onCloseBeautify,
  onBeautify,
}: HudShortcutsProps) {
  const style: CSSProperties = {
    ...overlayBase,
    right: 16,
    bottom: 16,
    padding: "8px 12px",
    fontFamily: "var(--pir-font-mono)",
    fontSize: 10,
    color: "var(--pir-text-muted)",
    display: "flex",
    alignItems: "center",
    gap: 10,
  };
  return (
    <div style={style} onPointerDown={stopPointerDown}>
      <Sc k="⌥drag">pin</Sc>
      <Sc k="esc">deselect</Sc>
      <Sc k="wheel">zoom</Sc>
      <Sc k="dbl">fit</Sc>
      <span style={{ width: 1, height: 14, background: "var(--pir-border)" }} />
      <BeautifyMenu
        open={beautifyOpen}
        onToggle={onToggleBeautify}
        onClose={onCloseBeautify}
        onBeautify={onBeautify}
      />
    </div>
  );
}

export const HudShortcuts = memo(HudShortcutsImpl);

// -----------------------------------------------------------------------------
// BeautifyToast — separato perche' gestito da RAF cleanup parent
// -----------------------------------------------------------------------------

/** @lintignore — props toast, consumata solo internamente da GraphCanvas. */
export interface BeautifyToastProps {
  label: string;
  reducedMotion: boolean;
}

function BeautifyToastImpl({ label, reducedMotion }: BeautifyToastProps) {
  const style: CSSProperties = {
    position: "absolute",
    left: "50%",
    top: 90,
    transform: "translateX(-50%)",
    padding: "8px 14px",
    background: "hsl(var(--pir-accent) / 0.96)",
    color: "white",
    border: "1px solid hsl(var(--pir-accent))",
    borderRadius: 2,
    fontFamily: "var(--pir-font-mono)",
    fontSize: 10,
    letterSpacing: "0.16em",
    textTransform: "uppercase",
    fontWeight: 600,
    pointerEvents: "none",
    animation: reducedMotion ? undefined : "beautifyFade 2s ease-out forwards",
    zIndex: 20,
  };
  return <div style={style}>{label}</div>;
}

export const BeautifyToast = memo(BeautifyToastImpl);
