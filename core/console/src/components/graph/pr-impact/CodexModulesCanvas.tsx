// v4.0.0 - 2026-05-17 - Parita Cosmo completa (PR7).
//
// Replica del pattern Cosmo GraphCanvas su Codex:
//  - wheel listener inline (no piu' usePanZoom hook) con rAF + passive:false,
//    zoom-at-cursor con math letterale Cosmo
//  - tween RAF per Beautify (constellation/galaxy/grappolo/sistema-solare)
//    con ease in-out cubic 700ms
//  - Alt+drag con force repulsion LIVE durante move (non solo al release)
//  - Arc-label con arc-stroke che maschera il bordo del cerchio (pattern
//    cosmo `renderArcLabel` letterale)
//  - 4-tier LOD hysteresis aligned a SAT_LOD_THRESHOLDS Cosmo [8,12,14,27,30]
//  - Fit hardcoded a INITIAL_ZOOM/{0,0} (era reset() da usePanZoom che leggeva
//    initialZoom dinamico dal LS)
//
// containerRef + zoomRef/panRef sono refs locali (non da hook esterno) per
// massima fidelity al pattern Cosmo che funziona.
"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

import {
  CODEX_TIERS,
  forceLayout,
  lodOpacity,
  packFibonacci,
  resolveOverlaps,
  type ForceEdge,
  type ForceNode,
  type OverlapItem,
  type PackedCircle,
} from "../_engine";
import { CodexBeautifyMenu } from "./CodexBeautifyMenu";
import {
  layoutCodexConstellation,
  layoutCodexGalaxy,
  layoutCodexGrappolo,
  layoutCodexSistemaSolare,
  type CodexBeautifyKind,
} from "./codexLayouts";
import {
  CLUSTER_COLORS,
  type CodexClusterId,
  type CodexModuleEdgeItem,
  type CodexModuleItem,
} from "./types";
import { useCodexViewState, type CodexOverride } from "./useCodexViewState";

const WORLD_W = 1800;
const WORLD_H = 1100;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4;
const INITIAL_ZOOM = 0.7;
const EPS = 1e-4;
const TWEEN_MS = 700;
const TOAST_MS = 1800;

// LOD thresholds — allineati Cosmo SAT_LOD_THRESHOLDS [8, 12, 14, 27, 30, 60]
// effR = sat.r * zoom (raggio sullo schermo in pixel)
const LOD_SAT_VISIBLE = 6;        // sotto: niente render satellite
const LOD_SAT_LABEL_BASE = 8;     // sotto: nessuna label/icon
const LOD_MID = 14;               // fib-number / count-badge appare
const LOD_ARC_FADE_LO = 27;       // arc-label fade-in start
const LOD_ARC_FADE_HI = 30;       // arc-label full opacity
const LOD_DEEP = 60;              // satellite cliccabile + 12 items
const LOD_PLANET_LABEL = 14;      // planet label fade-in

const PAN_LAYER_SKIP_ATTR = "data-codex-planet";
const CONTROLS_SKIP_ATTR = "data-codex-controls";

const FUN_PACKING: readonly PackedCircle[] = packFibonacci(CODEX_TIERS, 0.92);

function clampZoom(z: number): number {
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
}

function moduleRadius(degree: number, functionCount: number): number {
  const effective = degree > 0 ? degree : functionCount / 4;
  return Math.max(36, Math.min(120, Math.pow(effective, 0.55) * 6.5));
}

interface PlacedModule extends CodexModuleItem {
  readonly id: string;
  readonly r: number;
  readonly x: number;
  readonly y: number;
}

const EDGE_STYLE: Record<
  CodexModuleEdgeItem["relation"],
  { stroke: string; dash?: string }
> = {
  calls: { stroke: "hsl(var(--bone-200))" },
  imports: { stroke: "hsl(204 70% 60%)" },
  depends_on: { stroke: "hsl(290 60% 65%)" },
  mentions: { stroke: "hsl(var(--bone-200))", dash: "4 3" },
};

const BEAUTIFY_LABELS: Readonly<Record<CodexBeautifyKind, string>> = {
  constellation: "CONSTELLATION · orbite per cluster",
  galaxy: "GALAXY · spirali per cluster",
  grappolo: "GRAPPOLO · cluster denso",
  "sistema-solare": "SISTEMA SOLARE · orbite larghe",
  reset: "RESET · posizioni cancellate",
};

export interface CodexModulesCanvasProps {
  modules: CodexModuleItem[];
  edges: CodexModuleEdgeItem[];
  project: string;
  selectedSlug: string | null;
  /** Slug di moduli connessi (via edges) al selected. Vuoto = no highlight mode. */
  connectedSlugs?: ReadonlySet<string>;
  /** Single-click su pianeta: highlight selected + correlati (no entry). */
  onSelect: (slug: string) => void;
  /** Double-click su pianeta: entry vista funzioni del modulo. */
  onActivate?: (slug: string) => void;
}

export function CodexModulesCanvas({
  modules,
  edges,
  project,
  selectedSlug,
  connectedSlugs,
  onActivate,
  onSelect,
}: CodexModulesCanvasProps) {
  // ===== LAYOUT BASE (force-directed grappolo iniziale) =====
  const placedBase = useMemo<PlacedModule[]>(() => {
    const nodes = modules.map<ForceNode & CodexModuleItem & { id: string }>((m) => ({
      ...m,
      id: m.slug,
      r: moduleRadius(m.degree, m.function_count),
    }));
    const forceEdges: ForceEdge[] = edges.map((e) => ({
      source: e.source,
      target: e.target,
      weight: e.weight,
    }));
    const result = forceLayout(nodes, forceEdges, {
      REPULSE: 2000,
      SPRING_K: 0.07,
      SPRING_L: 10,
      GRAVITY: 0.030,
      DAMPING: 0.82,
      MIN_GAP: 2,
      iterations: 400,
      seed: 73,
      finalCollisionPasses: 15,
      viewport: { w: WORLD_W, h: WORLD_H },
      seedOrder: (a, b) => {
        const ka = a.degree || a.function_count;
        const kb = b.degree || b.function_count;
        if (kb !== ka) return kb - ka;
        return a.id.localeCompare(b.id);
      },
    });
    return result as PlacedModule[];
  }, [modules, edges]);

  // ===== VIEW STATE (LS persistente: zoom/pan/nodeOverrides) =====
  const { state, patch, clearOverrides } = useCodexViewState();

  // ===== POSIZIONI FINALI: base + overrides =====
  const placedFinal = useMemo<PlacedModule[]>(() => {
    if (Object.keys(state.nodeOverrides).length === 0) return placedBase;
    return placedBase.map((m) => {
      const ov = state.nodeOverrides[m.slug];
      return ov ? { ...m, x: ov.x, y: ov.y } : m;
    });
  }, [placedBase, state.nodeOverrides]);

  const byId = useMemo<Record<string, PlacedModule>>(
    () => Object.fromEntries(placedFinal.map((m) => [m.slug, m])),
    [placedFinal],
  );

  // ===== PAN/ZOOM: pattern Cosmo inline (no hook) =====
  const containerRef = useRef<HTMLDivElement | null>(null);
  const zoomRef = useRef(state.zoom);
  const panRef = useRef(state.pan);
  useEffect(() => {
    zoomRef.current = state.zoom;
  }, [state.zoom]);
  useEffect(() => {
    panRef.current = state.pan;
  }, [state.pan]);
  const zoom = state.zoom;
  const pan = state.pan;

  // ===== TWEEN STATE =====
  const animRef = useRef<number | null>(null);
  const toastTimer = useRef<number | null>(null);
  const [beautifyToast, setBeautifyToast] = useState<string | null>(null);

  const cancelAnim = useCallback(() => {
    if (animRef.current !== null) {
      cancelAnimationFrame(animRef.current);
      animRef.current = null;
    }
  }, []);
  const clearToast = useCallback(() => {
    if (toastTimer.current !== null) {
      window.clearTimeout(toastTimer.current);
      toastTimer.current = null;
    }
  }, []);
  useEffect(() => () => clearToast(), [clearToast]);

  // ===== WHEEL (pattern Cosmo: rAF + passive:false + zoom-at-cursor) =====
  // useLayoutEffect garantisce che il listener sia attaccato dopo che React
  // ha commitato il ref ma prima del paint. Con useEffect, in alcuni casi
  // (StrictMode double-invoke o re-mount) il listener veniva attaccato troppo
  // tardi e i primi wheel events andavano persi.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;
    const pending = { deltaY: 0, mx: 0, my: 0, hasMouse: false };
    let rafId: number | null = null;

    const flush = () => {
      rafId = null;
      if (pending.deltaY === 0) return;
      const accumulated = pending.deltaY;
      pending.deltaY = 0;
      cancelAnim();
      const currentZoom = zoomRef.current;
      const currentPan = panRef.current;
      const delta = -accumulated * 0.0015;
      const factor = 1 + delta;
      const newZoom = clampZoom(currentZoom * factor);
      if (Math.abs(newZoom - currentZoom) < EPS) return;
      if (!pending.hasMouse) {
        patch({ zoom: newZoom });
        return;
      }
      // Zoom-at-cursor con transform `translate(panX, panY) scale(zoom)` +
      // transformOrigin WORLD_W/2, WORLD_H/2:
      //   sx = WORLD_W/2 + (wx - WORLD_W/2)*zoom + panX
      // Vogliamo che il punto world (wx,wy) sotto al cursore resti sotto
      // (mx, my) anche con newZoom:
      //   wx = (pending.mx - currentPan.x - WORLD_W/2) / currentZoom + WORLD_W/2
      //   newPanX = pending.mx - (wx - WORLD_W/2)*newZoom - WORLD_W/2
      const wx = (pending.mx - currentPan.x - WORLD_W / 2) / currentZoom + WORLD_W / 2;
      const wy = (pending.my - currentPan.y - WORLD_H / 2) / currentZoom + WORLD_H / 2;
      const newPanX = pending.mx - (wx - WORLD_W / 2) * newZoom - WORLD_W / 2;
      const newPanY = pending.my - (wy - WORLD_H / 2) * newZoom - WORLD_H / 2;
      patch({ zoom: newZoom, pan: { x: newPanX, y: newPanY } });
    };

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      pending.deltaY += e.deltaY;
      pending.mx = e.clientX - rect.left;
      pending.my = e.clientY - rect.top;
      pending.hasMouse = true;
      if (rafId === null) {
        rafId = window.requestAnimationFrame(flush);
      }
    };

    el.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      el.removeEventListener("wheel", onWheel);
      if (rafId !== null) window.cancelAnimationFrame(rafId);
    };
  }, [patch, cancelAnim]);

  // ===== PAN POINTER (commit-on-move, skip controls/button) =====
  const panStartRef = useRef<{
    clientX: number;
    clientY: number;
    panX: number;
    panY: number;
  } | null>(null);

  // Alt+drag state
  const dragRef = useRef<{ slug: string; offsetWx: number; offsetWy: number } | null>(null);

  // Math screen→world per il transform Cosmo-style:
  //   transform: translate(panX, panY) scale(zoom)
  //   transformOrigin: WORLD_W/2 WORLD_H/2
  // screen px del world point (wx, wy):
  //   sx = WORLD_W/2 + (wx - WORLD_W/2)*zoom + panX
  // inversa:
  //   wx = (sx - panX - WORLD_W/2) / zoom + WORLD_W/2
  const screenToWorld = useCallback(
    (clientX: number, clientY: number): { wx: number; wy: number } | null => {
      const el = containerRef.current;
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      const z = zoomRef.current;
      const p = panRef.current;
      const sx = clientX - rect.left;
      const sy = clientY - rect.top;
      const wx = (sx - p.x - WORLD_W / 2) / z + WORLD_W / 2;
      const wy = (sy - p.y - WORLD_H / 2) / z + WORLD_H / 2;
      return { wx, wy };
    },
    [],
  );

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) return;
      const target = e.target as HTMLElement;
      if (target.closest(`[${PAN_LAYER_SKIP_ATTR}]`)) return;
      if (target.closest(`[${CONTROLS_SKIP_ATTR}]`)) return;
      if (target.closest("button")) return;
      cancelAnim();
      panStartRef.current = {
        clientX: e.clientX,
        clientY: e.clientY,
        panX: panRef.current.x,
        panY: panRef.current.y,
      };
    },
    [cancelAnim],
  );

  const onPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      // Alt+drag priority: planet move + live force repulsion
      const drag = dragRef.current;
      if (drag) {
        const w = screenToWorld(e.clientX, e.clientY);
        if (!w) return;
        const newX = w.wx - drag.offsetWx;
        const newY = w.wy - drag.offsetWy;
        // Build items map from placedFinal con override pianeta draggato
        const items: Array<OverlapItem & { slug: string }> = placedFinal.map((m) => {
          if (m.slug === drag.slug) {
            return { slug: m.slug, x: newX, y: newY, r: m.r };
          }
          const ov = state.nodeOverrides[m.slug];
          return {
            slug: m.slug,
            x: ov?.x ?? m.x,
            y: ov?.y ?? m.y,
            r: m.r,
          };
        });
        // Force repulsion live: pochi pass per fluidity (release fa pass piu fitti)
        resolveOverlaps(items, { minGap: 4, passes: 4 });
        const updates: Record<string, CodexOverride> = {};
        for (const it of items) updates[it.slug] = { x: it.x, y: it.y };
        patch({ nodeOverrides: updates });
        return;
      }
      // Pan canvas — delta in screen px (no diviso zoom: il transform
      // `translate(pan.x, pan.y)` applica i pixel screen direttamente, dopo
      // lo scale del world). Equivalente a Cosmo GraphCanvas pan.
      const start = panStartRef.current;
      if (!start) return;
      patch({
        pan: {
          x: start.panX + (e.clientX - start.clientX),
          y: start.panY + (e.clientY - start.clientY),
        },
      });
    },
    [patch, placedFinal, state.nodeOverrides, screenToWorld],
  );

  const onPointerUp = useCallback(() => {
    panStartRef.current = null;
    if (dragRef.current) {
      // Final assessment: piu pass per garanzia zero overlap
      const items: Array<OverlapItem & { slug: string }> = placedFinal.map((m) => {
        const ov = state.nodeOverrides[m.slug];
        return {
          slug: m.slug,
          x: ov?.x ?? m.x,
          y: ov?.y ?? m.y,
          r: m.r,
        };
      });
      resolveOverlaps(items, { minGap: 4, passes: 15 });
      const updates: Record<string, CodexOverride> = {};
      for (const it of items) updates[it.slug] = { x: it.x, y: it.y };
      patch({ nodeOverrides: updates });
      dragRef.current = null;
    }
  }, [patch, placedFinal, state.nodeOverrides]);

  const onPlanetPointerDown = useCallback(
    (e: ReactPointerEvent<SVGGElement>, m: PlacedModule) => {
      if (!e.altKey) return;
      e.stopPropagation();
      const w = screenToWorld(e.clientX, e.clientY);
      if (!w) return;
      (e.currentTarget as SVGGElement).setPointerCapture(e.pointerId);
      dragRef.current = {
        slug: m.slug,
        offsetWx: w.wx - m.x,
        offsetWy: w.wy - m.y,
      };
      cancelAnim();
    },
    [cancelAnim, screenToWorld],
  );

  // ===== BEAUTIFY TWEEN =====
  const onBeautify = useCallback(
    (kind: CodexBeautifyKind) => {
      cancelAnim();
      clearToast();
      setBeautifyToast(BEAUTIFY_LABELS[kind]);
      toastTimer.current = window.setTimeout(() => setBeautifyToast(null), TOAST_MS);

      if (kind === "reset") {
        clearOverrides();
        return;
      }

      let target: Record<string, { x: number; y: number }>;
      if (kind === "constellation") target = layoutCodexConstellation(modules);
      else if (kind === "galaxy") target = layoutCodexGalaxy(modules);
      else if (kind === "grappolo") target = layoutCodexGrappolo(modules, edges);
      else target = layoutCodexSistemaSolare(modules, edges);

      const basePos: Record<string, { x: number; y: number }> = {};
      for (const m of placedBase) basePos[m.slug] = { x: m.x, y: m.y };
      const startOverrides = { ...state.nodeOverrides };
      const allSlugs = new Set<string>([
        ...Object.keys(startOverrides),
        ...Object.keys(target),
        ...modules.map((m) => m.slug),
      ]);
      const from: Record<string, { x: number; y: number }> = {};
      for (const slug of allSlugs) {
        from[slug] = startOverrides[slug] ?? basePos[slug] ?? { x: WORLD_W / 2, y: WORLD_H / 2 };
      }

      const startTime = performance.now();
      const animate = (now: number) => {
        const elapsed = now - startTime;
        const t = Math.min(1, elapsed / TWEEN_MS);
        const eased = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        const next: Record<string, CodexOverride> = {};
        for (const slug of allSlugs) {
          const f = from[slug];
          const tg = target[slug] ?? basePos[slug] ?? f;
          next[slug] = {
            x: f.x + (tg.x - f.x) * eased,
            y: f.y + (tg.y - f.y) * eased,
          };
        }
        patch({ nodeOverrides: next });
        if (t < 1) {
          animRef.current = requestAnimationFrame(animate);
        } else {
          animRef.current = null;
        }
      };
      animRef.current = requestAnimationFrame(animate);
    },
    [modules, edges, placedBase, state.nodeOverrides, patch, clearOverrides, cancelAnim, clearToast],
  );

  // ===== FIT: calcola bounding box reale + zoom/pan per fittare nel viewport =====
  const onFit = useCallback(() => {
    cancelAnim();
    const el = containerRef.current;
    if (!el || placedFinal.length === 0) {
      patch({ zoom: INITIAL_ZOOM, pan: { x: 0, y: 0 } });
      return;
    }
    const rect = el.getBoundingClientRect();
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const m of placedFinal) {
      if (m.x - m.r < minX) minX = m.x - m.r;
      if (m.x + m.r > maxX) maxX = m.x + m.r;
      if (m.y - m.r < minY) minY = m.y - m.r;
      if (m.y + m.r > maxY) maxY = m.y + m.r;
    }
    const bboxW = Math.max(1, maxX - minX);
    const bboxH = Math.max(1, maxY - minY);
    const bboxCx = (minX + maxX) / 2;
    const bboxCy = (minY + maxY) / 2;
    const margin = 0.88;
    const fitZoom = clampZoom(
      Math.min((rect.width * margin) / bboxW, (rect.height * margin) / bboxH),
    );
    // Pan tale che bbox center → screen center.
    // sx(bboxCx) = WORLD_W/2 + (bboxCx - WORLD_W/2)*fitZoom + panX
    // Vogliamo sx = rect.width/2 → panX = rect.width/2 - WORLD_W/2 - (bboxCx - WORLD_W/2)*fitZoom
    const panX = rect.width / 2 - WORLD_W / 2 - (bboxCx - WORLD_W / 2) * fitZoom;
    const panY = rect.height / 2 - WORLD_H / 2 - (bboxCy - WORLD_H / 2) * fitZoom;
    patch({ zoom: fitZoom, pan: { x: panX, y: panY } });
  }, [patch, cancelAnim, placedFinal]);

  const [hoveredSlug, setHoveredSlug] = useState<string | null>(null);

  return (
    <div
      ref={containerRef}
      style={SHELL_STYLE}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      <svg style={DOTS_STYLE} aria-hidden>
        <defs>
          <pattern id="codex-dots" width="28" height="28" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.7" fill="hsl(var(--bone-400))" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#codex-dots)" />
      </svg>

      <div
        style={{
          ...PAN_LAYER_STYLE,
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: `${WORLD_W / 2}px ${WORLD_H / 2}px`,
        }}
      >
        <svg
          width={WORLD_W}
          height={WORLD_H}
          viewBox={`0 0 ${WORLD_W} ${WORLD_H}`}
          style={{ position: "absolute", inset: 0, overflow: "visible" }}
        >
          {edges.map((e, i) => {
            const a = byId[e.source];
            const b = byId[e.target];
            if (!a || !b) return null;
            const style = EDGE_STYLE[e.relation];
            const baseOp = 0.3 + Math.min(1, e.weight / 12) * 0.5;
            const thickness = Math.max(0.8, Math.min(5, Math.pow(e.weight, 0.65) * 0.7));
            // Highlight mode: se selectedSlug set, dim gli edge che NON toccano
            // selected, evidenzia quelli connessi.
            const involvesSelected =
              selectedSlug !== null &&
              (e.source === selectedSlug || e.target === selectedSlug);
            const op = selectedSlug === null
              ? baseOp
              : involvesSelected ? Math.min(1, baseOp + 0.3) : baseOp * 0.25;
            const finalThickness = involvesSelected ? thickness * 1.4 : thickness;
            return (
              <line
                key={`e-${i}`}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={involvesSelected ? "hsl(var(--pir-accent))" : style.stroke}
                strokeOpacity={op}
                strokeWidth={finalThickness}
                strokeLinecap="round"
                strokeDasharray={style.dash}
              />
            );
          })}

          {placedFinal.map((m) => {
            const c = CLUSTER_COLORS[m.cluster];
            const isHovered = m.slug === hoveredSlug;
            const isSelected = m.slug === selectedSlug;
            const isConnected = connectedSlugs?.has(m.slug) ?? false;
            // Dim mode: selectedSlug attivo ma questo pianeta non e selected ne connected.
            const isDimmed = selectedSlug !== null && !isSelected && !isConnected;
            const fillBase = `hsl(${c.hue} ${c.sat}% ${Math.min(70, c.light + 6)}%)`;
            const fillFocus = `hsl(${c.hue} ${c.sat}% ${Math.min(78, c.light + 14)}%)`;
            const fill = isSelected || isHovered ? fillFocus : fillBase;
            const stroke = isSelected
              ? "hsl(var(--pir-accent))"
              : isConnected
              ? `hsl(${c.hue} ${c.sat}% ${Math.max(25, c.light - 18)}%)`
              : `hsl(${c.hue} ${c.sat}% ${Math.max(20, c.light - 28)}%)`;
            const strokeWidth = (isSelected ? 2.4 : isConnected ? 1.6 : 0.9) / zoom;
            const groupOpacity = isDimmed ? 0.25 : 1;
            const effR = m.r * zoom;
            const labelOpacity = lodOpacity(effR, LOD_PLANET_LABEL - 2, LOD_PLANET_LABEL + 2);
            const showSatellites = m.r >= 60 && zoom >= 0.55 && m.top_functions.length > 0;

            return (
              <g
                key={m.slug}
                {...{ [PAN_LAYER_SKIP_ATTR]: true }}
                transform={`translate(${m.x} ${m.y})`}
                style={{ cursor: dragRef.current?.slug === m.slug ? "grabbing" : "pointer" }}
                opacity={groupOpacity}
                onMouseEnter={() => setHoveredSlug(m.slug)}
                onMouseLeave={() => setHoveredSlug(null)}
                onPointerDown={(e) => onPlanetPointerDown(e, m)}
                onClick={(e) => {
                  e.stopPropagation();
                  if (e.altKey) return; // Alt+drag — no select
                  onSelect(m.slug);
                }}
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  if (onActivate) onActivate(m.slug);
                }}
              >
                <circle r={m.r} fill={fill} stroke={stroke} strokeWidth={strokeWidth} />

                {showSatellites && (
                  <SatellitePack
                    moduleSlug={m.slug}
                    moduleR={m.r}
                    topFunctions={m.top_functions}
                    cluster={m.cluster}
                    zoom={zoom}
                  />
                )}

                <text
                  x="0"
                  y={m.r >= 60 ? 3 / zoom : m.r * 0.05}
                  textAnchor="middle"
                  fontFamily="var(--pir-font-display, var(--pir-font-mono))"
                  fontSize={Math.min(m.r * 0.32, 22) / zoom}
                  fontWeight={700}
                  fill="hsl(var(--pir-base))"
                  opacity={labelOpacity}
                  style={{ letterSpacing: "-0.005em", pointerEvents: "none" }}
                >
                  {m.semantic_label || m.label}
                </text>
                {m.r >= 50 && (
                  <text
                    x="0"
                    y={m.r * 0.55}
                    textAnchor="middle"
                    fontFamily="var(--pir-font-mono)"
                    fontSize={Math.min(m.r * 0.17, 11) / zoom}
                    fontWeight={600}
                    fill="hsl(30 5% 18%)"
                    opacity={0.7 * labelOpacity}
                    style={{ letterSpacing: "0.06em", pointerEvents: "none" }}
                  >
                    {m.function_count}·fn · {m.degree}·edge
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      <div {...{ [CONTROLS_SKIP_ATTR]: true }} style={BREADCRUMB_STYLE}>
        <span style={{ cursor: "pointer" }}>CODEX</span>
        <span style={{ opacity: 0.5 }}>·</span>
        <span style={{ color: "var(--pir-text-primary)" }}>{modules.length}</span>
        <span> MODULES</span>
        <span style={{ opacity: 0.5 }}>·</span>
        <span style={{ color: "var(--pir-text-primary)" }}>{edges.length}</span>
        <span> EDGES</span>
        <span style={{ opacity: 0.5, marginLeft: 8 }}>·</span>
        <span style={{ color: "hsl(var(--pir-accent))", textTransform: "none", letterSpacing: "0.02em" }}>
          {project}
        </span>
      </div>

      <div {...{ [CONTROLS_SKIP_ATTR]: true }} style={LEGEND_STYLE}>
        <div style={LEGEND_TITLE}>Cluster semantici</div>
        <div style={LEGEND_CHIPS}>
          {(Object.keys(CLUSTER_COLORS) as CodexClusterId[]).map((id) => {
            const c = CLUSTER_COLORS[id];
            return (
              <span key={id} style={LEGEND_CHIP}>
                <span
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    background: `hsl(${c.hue} ${c.sat}% ${c.light + 6}%)`,
                    border: `1px solid hsl(${c.hue} ${c.sat}% ${c.light - 25}%)`,
                    display: "inline-block",
                  }}
                />
                <span style={{ fontSize: 9.5 }}>{c.label}</span>
              </span>
            );
          })}
        </div>
        <div style={LEGEND_FOOT}>
          <div>● size ∝ degree · ⚠ drift count</div>
          <div>━ calls · imports · -- depends_on</div>
        </div>
      </div>

      <div {...{ [CONTROLS_SKIP_ATTR]: true }} style={ZOOM_CTRL_STYLE}>
        <CodexBeautifyMenu onBeautify={onBeautify} />
        <span style={ZOOM_DIVIDER} />
        <button style={ZOOM_BTN} onClick={onFit}>fit</button>
        <span style={ZOOM_DIVIDER} />
        <span style={{ minWidth: 50, textAlign: "center", alignSelf: "center" }}>
          {Math.round(zoom * 100)}%
        </span>
      </div>

      <div {...{ [CONTROLS_SKIP_ATTR]: true }} style={HINT_STYLE}>
        Alt+drag pianeta · sposta + push vicini live
      </div>

      {beautifyToast && (
        <div {...{ [CONTROLS_SKIP_ATTR]: true }} style={TOAST_STYLE}>
          {beautifyToast}
        </div>
      )}
    </div>
  );
}

// ===== Satellite rendering =====

function SatellitePack({
  moduleSlug,
  moduleR,
  topFunctions,
  cluster,
  zoom,
}: {
  moduleSlug: string;
  moduleR: number;
  topFunctions: string[];
  cluster: CodexClusterId;
  zoom: number;
}) {
  const c = CLUSTER_COLORS[cluster];
  return (
    <g>
      {FUN_PACKING.slice(0, topFunctions.length).map((s, i) => {
        const satR = s.r * moduleR * 0.95;
        const effSatR = satR * zoom;
        if (effSatR < LOD_SAT_VISIBLE) return null;
        const sx = s.x * moduleR * 0.95;
        const sy = s.y * moduleR * 0.95;
        const name = topFunctions[i] ?? "";
        const short =
          name.replace(/^py:function:/, "").replace(/^ts:function:/, "").split(".").pop() ?? name;

        // LOD multi-tier con hysteresis crossfade:
        //   - LOD_MID (14): count badge appare
        //   - LOD_ARC_FADE_LO→HI (27-30): arc-label fade-in
        //   - LOD_DEEP (60): isClickable, items expandable
        const midOpacity = lodOpacity(effSatR, LOD_MID - 2, LOD_MID + 2);
        const arcOp = lodOpacity(effSatR, LOD_ARC_FADE_LO, LOD_ARC_FADE_HI);
        const fibOp = effSatR >= LOD_SAT_LABEL_BASE ? Math.max(0, 1 - arcOp) * midOpacity : 0;
        const arcId = `codex-arc-${moduleSlug}-${i}`;
        const isClickable = effSatR >= LOD_DEEP;

        const lightFill = Math.min(80, c.light + 12);
        const lightStroke = Math.max(15, c.light - 25);

        return (
          <g key={i} transform={`translate(${sx} ${sy})`}>
            <circle
              r={satR}
              fill={`hsl(${c.hue} ${c.sat}% ${lightFill}%)`}
              stroke={`hsl(${c.hue} ${c.sat}% ${lightStroke}%)`}
              strokeWidth={0.6 / zoom}
              style={{ cursor: isClickable ? "pointer" : "default" }}
            />

            {arcOp > 0 && (
              <ArcLabel
                arcId={arcId}
                satR={satR}
                zoom={zoom}
                opacity={arcOp}
                text={short}
                clusterHue={c.hue}
                clusterSat={c.sat}
                clusterLightFill={lightFill}
              />
            )}

            {fibOp > 0 && (
              <text
                x="0"
                y={satR + 8 / zoom}
                textAnchor="middle"
                fontFamily="var(--pir-font-mono)"
                fontSize={Math.min(satR * 0.45, 11) / zoom}
                fontWeight={500}
                fill="hsl(var(--pir-text-primary))"
                opacity={fibOp}
                style={{ letterSpacing: "0.005em", pointerEvents: "none" }}
              >
                {short.length > 18 ? short.slice(0, 17) + "…" : short}
              </text>
            )}
          </g>
        );
      })}
    </g>
  );
}

/**
 * Arc-label SVG textPath sul rim interno del satellite.
 * Porta letterale da cosmo/GraphCanvas.tsx renderArcLabel (commit 8674cbf):
 * un arc-stroke sul rim ESTERNO maschera il bordo del cerchio sotto al testo,
 * poi textPath disegna il testo sul rim interno.
 */
function ArcLabel({
  arcId,
  satR,
  zoom,
  opacity,
  text,
  clusterHue,
  clusterSat,
  clusterLightFill,
}: {
  arcId: string;
  satR: number;
  zoom: number;
  opacity: number;
  text: string;
  clusterHue: number;
  clusterSat: number;
  clusterLightFill: number;
}): ReactNode {
  const fontSizeScreen = 12;
  const fontSize = fontSizeScreen / zoom;
  const innerR = satR - fontSize * 1.0;
  if (innerR <= 2) return null;
  const avgCharW = fontSize * 0.56;
  const maxSpan = (140 * Math.PI) / 180;
  const arcLen = innerR * maxSpan;
  const maxChars = Math.max(3, Math.floor(arcLen / avgCharW));
  const labelText = text.toUpperCase();
  const fitted =
    labelText.length > maxChars ? labelText.slice(0, maxChars - 1) + "…" : labelText;
  const textArcLen = fitted.length * avgCharW;
  const centerAng = -Math.PI / 2;
  const half = Math.min(maxSpan / 2, textArcLen / (innerR * 2) + 0.1);
  const tx1 = Math.cos(centerAng - half) * innerR;
  const ty1 = Math.sin(centerAng - half) * innerR;
  const tx2 = Math.cos(centerAng + half) * innerR;
  const ty2 = Math.sin(centerAng + half) * innerR;
  const maskHalf = half + 0.05;
  const mx1 = Math.cos(centerAng - maskHalf) * satR;
  const my1 = Math.sin(centerAng - maskHalf) * satR;
  const mx2 = Math.cos(centerAng + maskHalf) * satR;
  const my2 = Math.sin(centerAng + maskHalf) * satR;

  const maskStroke = `hsl(${clusterHue} ${clusterSat}% ${clusterLightFill}%)`;
  const textFill = "hsl(var(--pir-text-primary))";

  return (
    <>
      {/* arc-stroke che maschera il bordo del cerchio sotto al testo */}
      <path
        d={`M ${mx1} ${my1} A ${satR} ${satR} 0 0 1 ${mx2} ${my2}`}
        fill="none"
        stroke={maskStroke}
        strokeWidth={2.0 / zoom}
        strokeLinecap="butt"
        opacity={opacity}
      />
      <defs>
        <path
          id={arcId}
          d={`M ${tx1} ${ty1} A ${innerR} ${innerR} 0 0 1 ${tx2} ${ty2}`}
          fill="none"
        />
      </defs>
      <text
        fontFamily="var(--pir-font-mono)"
        fontSize={fontSize}
        fontWeight={700}
        fill={textFill}
        opacity={opacity}
        letterSpacing="0.02em"
        style={{ pointerEvents: "none" }}
      >
        <textPath href={`#${arcId}`} startOffset="50%" textAnchor="middle">
          {fitted}
        </textPath>
      </text>
    </>
  );
}

// ===== Styles =====

const SHELL_STYLE: CSSProperties = {
  position: "absolute",
  inset: 0,
  background: "hsl(var(--pir-base))",
  overflow: "hidden",
  cursor: "grab",
  touchAction: "none",
  userSelect: "none",
};

const DOTS_STYLE: CSSProperties = {
  position: "absolute",
  inset: 0,
  opacity: 0.07,
  pointerEvents: "none",
  width: "100%",
  height: "100%",
};

const PAN_LAYER_STYLE: CSSProperties = {
  position: "absolute",
  inset: 0,
  transformOrigin: "50% 50%",
};

const BREADCRUMB_STYLE: CSSProperties = {
  position: "absolute",
  top: 14,
  right: 16,
  padding: "7px 12px",
  background: "hsl(var(--pir-surface-0) / 0.92)",
  backdropFilter: "blur(6px)",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  fontFamily: "var(--pir-font-mono)",
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
  color: "var(--pir-text-tertiary)",
  display: "flex",
  gap: 8,
  alignItems: "center",
  zIndex: 5,
};

const LEGEND_STYLE: CSSProperties = {
  position: "absolute",
  bottom: 14,
  left: 16,
  padding: "9px 12px",
  background: "hsl(var(--pir-surface-0) / 0.92)",
  backdropFilter: "blur(6px)",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  fontFamily: "var(--pir-font-mono)",
  fontSize: 10,
  color: "var(--pir-text-tertiary)",
  display: "flex",
  flexDirection: "column",
  gap: 5,
  minWidth: 220,
  zIndex: 5,
};

const LEGEND_TITLE: CSSProperties = {
  fontWeight: 700,
  letterSpacing: "0.18em",
  textTransform: "uppercase",
  color: "var(--pir-text-muted)",
};

const LEGEND_CHIPS: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 6,
};

const LEGEND_CHIP: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
};

const LEGEND_FOOT: CSSProperties = {
  marginTop: 6,
  paddingTop: 6,
  borderTop: "1px solid var(--pir-border)",
  color: "var(--pir-text-muted)",
};

const ZOOM_CTRL_STYLE: CSSProperties = {
  position: "absolute",
  bottom: 14,
  right: 16,
  display: "flex",
  alignItems: "center",
  gap: 4,
  padding: "6px 8px",
  background: "hsl(var(--pir-surface-0) / 0.92)",
  backdropFilter: "blur(6px)",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  fontFamily: "var(--pir-font-mono)",
  fontSize: 10,
  color: "var(--pir-text-secondary)",
  zIndex: 5,
};

const ZOOM_BTN: CSSProperties = {
  width: 36,
  height: 22,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  background: "transparent",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  color: "var(--pir-text-secondary)",
  fontFamily: "var(--pir-font-mono)",
  fontSize: 11,
  fontWeight: 700,
  cursor: "pointer",
};

const ZOOM_DIVIDER: CSSProperties = {
  width: 1,
  height: 14,
  background: "var(--pir-border)",
  margin: "0 2px",
  alignSelf: "center",
};

const HINT_STYLE: CSSProperties = {
  position: "absolute",
  top: 14,
  right: 16,
  padding: "5px 10px",
  background: "hsl(var(--pir-surface-0) / 0.7)",
  backdropFilter: "blur(4px)",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  fontFamily: "var(--pir-font-mono)",
  fontSize: 9,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: "var(--pir-text-muted)",
  pointerEvents: "none",
  zIndex: 5,
};

const TOAST_STYLE: CSSProperties = {
  position: "absolute",
  top: "50%",
  left: "50%",
  transform: "translate(-50%, -50%)",
  padding: "10px 18px",
  background: "hsl(var(--pir-surface-0) / 0.96)",
  backdropFilter: "blur(8px)",
  border: "1px solid hsl(var(--pir-accent))",
  borderRadius: 2,
  fontFamily: "var(--pir-font-mono)",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.18em",
  textTransform: "uppercase",
  color: "hsl(var(--pir-accent))",
  pointerEvents: "none",
  zIndex: 20,
  boxShadow: "0 6px 24px hsl(0 0% 0% / 0.20)",
};
