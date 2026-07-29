// v1.2.0 - 2026-04-27 - PR #21: prop searchQuery/searchMatches + dim non-match (project 0.15 / edge 0.05)
// v1.1.0 - 2026-04-24 - file-dots LOD 3-tier (count number / dot+N / clickable items)
// v1.0.0 - 2026-04-24 - Canvas SVG Cosmo (pan/zoom/drag/satellites/LOD).
//
// Porta di reference-graph-v1-cosmo.html righe 562-1432 con i fix M-FE-*:
//  M-FE-01  CSS transform su wrapper <g> + drag commit-on-release.
//  M-FE-03  LS write debounced (hook `useGraphViewState`).
//  M-FE-04  Unified animRef (animateView + beautify condividono).
//  M-FE-06  wheel via addEventListener {passive:false}.
//  M-FE-07  viewport guard 400x300.
//  M-FE-12  safeHref utility (usata per eventuali future finder link).
//  M-FE-13  zero dangerouslySetInnerHTML.
//  M-FE-15  Esc listener — lives in GraphPage (single owner).
//  M-FE-17  prefers-reduced-motion gate (animateView + beautify swap istantaneo).
//
// U-02 Click canvas vuoto cancella selezione.
// D-03 pan/zoom utente cancella tween beautify in corso.
// D-04 ResizeObserver debounce 150ms.
// H-04 MIN_GAP uniforme 10 (via forceLayoutHelpers).
// H-09 Skeleton mentre placedBase non e' pronto.
"use client";

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { forceLayout } from "./layouts/forceLayout";
import { layoutConstellation } from "./layouts/layoutConstellation";
import { layoutGrappolo } from "./layouts/layoutGrappolo";
import { layoutSistemaSolare } from "./layouts/layoutSistemaSolare";
import { layoutGalaxyArms } from "./layouts/layoutGalaxyArms";
import { layoutSatellitesFib } from "./layouts/satellitesFibonacci";
import { LAYOUT_VIEWPORT } from "./layouts/forceLayoutHelpers";
import { useGraphViewState } from "./useGraphViewState";
import { useReducedMotion } from "./useReducedMotion";
import {
  BeautifyToast,
  HudBreadcrumb,
  HudFilters,
  HudLegend,
  HudSearch,
  HudShortcuts,
  HudZoom,
} from "./Hud";
import type {
  BeautifyKind,
  Edge,
  Kind,
  Override,
  PlacedNode,
  Project,
  SatelliteItem,
} from "./types";

// -----------------------------------------------------------------------------
// Costanti
// -----------------------------------------------------------------------------

const ZOOM_MIN = 0.2;
const ZOOM_MAX = 24;
const EPS = 1e-6;
const RESIZE_DEBOUNCE_MS = 150;
const ANIMATE_VIEW_MS = 420;
const BEAUTIFY_MS = 800;
const TOAST_MS = 2000;
const DRAG_REPULSION_GAP = 4; // uniforme con MIN_GAP (grappolo d'uva, 2026-05-16)
const EDGE_STROKE = "hsl(var(--bone-200))";

const KIND_LIST: readonly Kind[] = [
  "plan",
  "brainstorm",
  "solution",
  "audit",
  "research",
  "handoff",
  "task",
  "learning",
];

const BEAUTIFY_LABELS: Readonly<Record<BeautifyKind, string>> = {
  constellation: "CONSTELLATION · orbite concentriche",
  galaxy: "GALAXY ARMS · spirali per program",
  grappolo: "GRAPPOLO · cluster denso",
  "sistema-solare": "SISTEMA SOLARE · orbite larghe",
  reset: "RESET · posizioni cancellate",
};

interface Viewport {
  w: number;
  h: number;
}

interface NodeDragState {
  slug: string;
  offsetX: number;
  offsetY: number;
}

interface AnimToken {
  canceled: boolean;
}

export interface SelectedDir {
  projectSlug: string;
  dirIdx: number;
  /** Kind del satellite selezionato (plan, handoff, audit, …). Guida filtering
   * inspector: mostra solo docs del project che matchano questo kind. */
  kind: Kind;
  name: string;
}

/** @lintignore — interfaccia props consumata solo internamente da GraphPage. */
export interface GraphCanvasProps {
  projects: readonly Project[];
  edges: readonly Edge[];
  selected: string | null;
  hovered: string | null;
  selectedDir: SelectedDir | null;
  showLabels: boolean;
  showSatellites: boolean;
  showEdges: boolean;
  /** Search input value (controlled). Empty = no search active. */
  searchQuery: string;
  /** Project slugs matching the active search; `null` quando nessuna search
   * attiva (tutti i nodi/edge full opacity). Set vuoto = search attiva ma
   * zero match → tutto dim. */
  searchMatches: ReadonlySet<string> | null;
  onSelect: (slug: string | null) => void;
  onHover: (slug: string | null) => void;
  onSelectDir: (dir: SelectedDir | null) => void;
  onToggleLabels: () => void;
  onToggleSatellites: () => void;
  onToggleEdges: () => void;
  onSearchQueryChange: (q: string) => void;
}

// -----------------------------------------------------------------------------
// Hook ResizeObserver con debounce (D-04)
// -----------------------------------------------------------------------------

function useDebouncedSize(ref: React.RefObject<HTMLElement | null>): Viewport {
  const [size, setSize] = useState<Viewport>({ w: 1180, h: 780 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    let timer: number | null = null;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize({ w: r.width, h: r.height });
    };
    // Misura iniziale immediata.
    measure();
    const ro = new ResizeObserver(() => {
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(measure, RESIZE_DEBOUNCE_MS);
    });
    ro.observe(el);
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      ro.disconnect();
    };
  }, [ref]);
  return size;
}

// -----------------------------------------------------------------------------
// Helper ease + matematica
// -----------------------------------------------------------------------------

function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

function easeInOutQuad(t: number): number {
  return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
}

function clampZoom(z: number): number {
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
}

/** Opacita' edge in funzione dello stato hover/select. */
function edgeOpacity(weight: number, active: boolean, hasActive: boolean): number {
  if (!hasActive) return 0.15 + Math.min(weight, 15) * 0.035;
  return active ? 0.9 : 0.05;
}

/** Numero di satelliti visibili per LOD.
 *
 * Tier calibrati per avere satelliti visibili anche a zoom base (1x). Con i
 * project medi (r ~ 22-30), a zoom 1 si ottiene effR ~ 24-30 → tier 24 entra,
 * 2 satelliti visibili. Sopra 48 cresciamo progressivamente.
 */
function maxSatellitesFor(effR: number): number {
  if (effR >= 120) return 8;
  if (effR >= 72) return 5;
  if (effR >= 48) return 3;
  return 2;
}

/** Stroke width del disco progetto (counter-scale con zoom). */
function projectStrokeWidth(isSelected: boolean, isHovered: boolean): number {
  if (isSelected) return 1.5;
  if (isHovered) return 1.2;
  return 0.8;
}

function projectFillColor(p: Pick<PlacedNode, "color">, isFocus: boolean): string {
  if (p.color) return p.color;
  return isFocus ? "hsl(var(--bone-100))" : "hsl(var(--bone-200))";
}

/** Stroke width del disco satellite. */
function satStrokeWidth(isDirSelected: boolean, isRecent: boolean): number {
  if (isDirSelected) return 1.6;
  if (isRecent) return 1.2;
  return 0.6;
}

// -----------------------------------------------------------------------------
// Sottocomponenti puri (memo-abili)
// -----------------------------------------------------------------------------

interface LabelProps {
  p: PlacedNode;
  zoom: number;
  isFocus: boolean;
  showLabels: boolean;
}

function ProjectLabel({ p, zoom, isFocus, showLabels }: LabelProps) {
  if (!showLabels) return null;
  // Gate zoom-aware (effPrjR = p.r * zoom): label appare solo per bubble
  // grandi on-screen. Threshold alzata a [32, 36] (grappolo v3, 2026-05-16):
  // a zoom=1 i progetti piccoli (r < 32) restano senza label per ridurre
  // confusione visiva; zoom-in li rivela. isFocus sovrascrive sempre.
  // Hysteresis crossfade 4px per evitare flicker durante zoom continuo.
  const effPrjR = p.r * zoom;
  const labelOpacity = isFocus
    ? 1
    : Math.max(0, Math.min(1, (effPrjR - 32) / 4));
  if (labelOpacity <= 0) return null;
  const style: CSSProperties = {
    position: "absolute",
    left: "50%",
    top: p.r * 2 + 5 / zoom,
    transform: `translateX(-50%) scale(${1 / zoom})`,
    transformOrigin: "top center",
    padding: isFocus ? "2px 6px" : "1px 4px",
    background: isFocus
      ? "hsl(var(--pir-surface-0))"
      : "hsl(var(--pir-base) / 0.7)",
    borderRadius: 2,
    fontFamily: "var(--pir-font-mono)",
    fontSize: p.r >= 40 ? 12 : 11,
    fontWeight: 500,
    color: isFocus ? "hsl(var(--pir-accent))" : "var(--pir-text-secondary)",
    whiteSpace: "nowrap",
    letterSpacing: "0.02em",
    pointerEvents: "none",
    opacity: labelOpacity,
  };
  return <div style={style}>project.{p.slug}</div>;
}

// -----------------------------------------------------------------------------
// GraphCanvas
// -----------------------------------------------------------------------------

function GraphCanvasImpl({
  projects,
  edges,
  selected,
  hovered,
  selectedDir,
  showLabels,
  showSatellites,
  showEdges,
  searchQuery,
  searchMatches,
  onSelect,
  onHover,
  onSelectDir,
  onToggleLabels,
  onToggleSatellites,
  onToggleEdges,
  onSearchQueryChange,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const viewport = useDebouncedSize(containerRef);

  const [{ zoom, pan, nodeOverrides }, patch] = useGraphViewState();
  const reducedMotion = useReducedMotion();

  // Ref mirror per accesso sincrono da listener/raf.
  const zoomRef = useRef(zoom);
  const panRef = useRef(pan);
  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);
  useEffect(() => {
    panRef.current = pan;
  }, [pan]);

  // Layout base: una singola computazione per tutto il ciclo di vita.
  const placedBase = useMemo<PlacedNode[]>(
    () => forceLayout(projects, LAYOUT_VIEWPORT, edges),
    [projects, edges],
  );

  // Applica overrides utente (pin + beautify finali).
  const placed = useMemo<PlacedNode[]>(() => {
    if (placedBase.length === 0) return placedBase;
    const hasOverrides = Object.keys(nodeOverrides).length > 0;
    if (!hasOverrides) return placedBase;
    return placedBase.map((p) => {
      const o = nodeOverrides[p.slug];
      return o ? { ...p, x: o.x, y: o.y } : p;
    });
  }, [placedBase, nodeOverrides]);

  const bySlug = useMemo<Record<string, PlacedNode>>(() => {
    const out: Record<string, PlacedNode> = {};
    for (const p of placed) out[p.slug] = p;
    return out;
  }, [placed]);

  const activeSlug = hovered ?? selected;

  // Set degli slug connessi ad activeSlug, O(edges).
  const connectedSlugs = useMemo<Set<string>>(() => {
    if (!activeSlug) return new Set();
    const s = new Set<string>();
    for (const e of edges) {
      if (e.source === activeSlug) s.add(e.target);
      else if (e.target === activeSlug) s.add(e.source);
    }
    return s;
  }, [activeSlug, edges]);

  // -----------------------------------------------------------------------
  // Pan / zoom / drag state (commit-on-release via ref, M-FE-01)
  // -----------------------------------------------------------------------
  const [dragging, setDragging] = useState(false);
  const [moved, setMoved] = useState(false);
  const panStart = useRef<{ clientX: number; clientY: number; pan: { x: number; y: number } } | null>(null);
  const nodeDragRef = useRef<NodeDragState | null>(null);

  // Unified animation token (M-FE-04): solo l'ultima tween attiva e' valida.
  const animRef = useRef<{ id: number | null; token: AnimToken }>({
    id: null,
    token: { canceled: false },
  });

  const cancelAnim = useCallback(() => {
    if (animRef.current.id !== null) {
      window.cancelAnimationFrame(animRef.current.id);
    }
    animRef.current.token.canceled = true;
    animRef.current = { id: null, token: { canceled: false } };
  }, []);

  useEffect(() => () => cancelAnim(), [cancelAnim]);

  // -----------------------------------------------------------------------
  // Sanity-check pan/zoom post-mount (evita stato orfano cambiando monitor).
  // useLayoutEffect (non useEffect): deve correggere zoom/pan PRIMA del first
  // paint per evitare il flicker iniziale (canvas con nodi off-screen visibili
  // per un frame prima del recenter).
  // -----------------------------------------------------------------------
  const sanitized = useRef(false);
  useLayoutEffect(() => {
    if (sanitized.current) return;
    if (viewport.w < 400 || viewport.h < 300) return; // M-FE-07
    if (placed.length === 0) return;
    sanitized.current = true;

    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const p of placed) {
      if (p.x - p.r < minX) minX = p.x - p.r;
      if (p.y - p.r < minY) minY = p.y - p.r;
      if (p.x + p.r > maxX) maxX = p.x + p.r;
      if (p.y + p.r > maxY) maxY = p.y + p.r;
    }
    const ox = viewport.w / 2;
    const oy = viewport.h / 2;
    const sMinX = ox + (minX - ox) * zoom + pan.x;
    const sMaxX = ox + (maxX - ox) * zoom + pan.x;
    const sMinY = oy + (minY - oy) * zoom + pan.y;
    const sMaxY = oy + (maxY - oy) * zoom + pan.y;
    const visW = Math.max(0, Math.min(sMaxX, viewport.w) - Math.max(sMinX, 0));
    const visH = Math.max(0, Math.min(sMaxY, viewport.h) - Math.max(sMinY, 0));
    const graphW = sMaxX - sMinX;
    const graphH = sMaxY - sMinY;
    const overlapFrac = (visW * visH) / Math.max(1, graphW * graphH);

    if (overlapFrac < 0.1 || zoom < 0.25 || zoom > 20) {
      const margin = 0.85;
      const fitZoom = Math.min(
        (viewport.w * margin) / Math.max(1, graphW / zoom),
        (viewport.h * margin) / Math.max(1, graphH / zoom),
      );
      const safeZoom = Math.max(0.3, Math.min(2, fitZoom));
      const cxWorld = (minX + maxX) / 2;
      const cyWorld = (minY + maxY) / 2;
      patch({
        zoom: safeZoom,
        pan: { x: -(cxWorld - ox) * safeZoom, y: -(cyWorld - oy) * safeZoom },
      });
    }
    // Deps: NO `zoom/pan/patch` per evitare re-fire ad ogni interazione utente.
    // `sanitized.current` e' single-shot, ma con quelle deps l'effect rifirava
    // ad ogni pan/zoom — a zoom alto produce flash dello schermo (recompute
    // bbox + condizione zoom>20 oscillante con il safeZoom appena patchato).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewport.w, viewport.h, placed]);

  // -----------------------------------------------------------------------
  // animateView — tween programmato (breadcrumb / dbl-click)
  // -----------------------------------------------------------------------
  const animateView = useCallback(
    (targetZoom: number, targetPan: { x: number; y: number }, duration = ANIMATE_VIEW_MS) => {
      cancelAnim();
      if (reducedMotion) {
        patch({ zoom: targetZoom, pan: targetPan });
        return;
      }
      const startZoom = zoomRef.current;
      const startPan = { ...panRef.current };
      const t0 = performance.now();
      const token = animRef.current.token;
      const step = (now: number) => {
        if (token.canceled) return;
        const t = Math.min(1, (now - t0) / duration);
        const k = easeInOutCubic(t);
        const z = startZoom + (targetZoom - startZoom) * k;
        const px = startPan.x + (targetPan.x - startPan.x) * k;
        const py = startPan.y + (targetPan.y - startPan.y) * k;
        patch({ zoom: z, pan: { x: px, y: py } });
        if (t < 1) {
          animRef.current.id = window.requestAnimationFrame(step);
        } else {
          animRef.current.id = null;
        }
      };
      animRef.current.id = window.requestAnimationFrame(step);
    },
    [cancelAnim, patch, reducedMotion],
  );

  // -----------------------------------------------------------------------
  // Beautify — tween overrides (condivide animRef, D-03)
  // -----------------------------------------------------------------------
  const [beautifyOpen, setBeautifyOpen] = useState(false);
  const [beautifyToast, setBeautifyToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);

  const clearToast = useCallback(() => {
    if (toastTimer.current !== null) {
      window.clearTimeout(toastTimer.current);
      toastTimer.current = null;
    }
  }, []);
  useEffect(() => () => clearToast(), [clearToast]);

  const runBeautify = useCallback(
    (kind: BeautifyKind) => {
      let target: Record<string, Override>;
      if (kind === "constellation") target = layoutConstellation(projects, edges);
      else if (kind === "galaxy") target = layoutGalaxyArms(projects);
      else if (kind === "grappolo") target = layoutGrappolo(projects, edges);
      else if (kind === "sistema-solare") target = layoutSistemaSolare(projects, edges);
      else target = {}; // reset

      const basePos: Record<string, Override> = {};
      for (const p of placedBase) basePos[p.slug] = { x: p.x, y: p.y };

      const startOverrides = { ...nodeOverrides };
      const allSlugs = new Set<string>([
        ...Object.keys(startOverrides),
        ...Object.keys(target),
        ...projects.map((p) => p.slug),
      ]);

      const from: Record<string, Override> = {};
      for (const slug of allSlugs) {
        from[slug] = startOverrides[slug] ?? basePos[slug] ?? { x: 900, y: 550 };
      }

      cancelAnim();
      clearToast();
      setBeautifyToast(BEAUTIFY_LABELS[kind]);
      toastTimer.current = window.setTimeout(() => setBeautifyToast(null), TOAST_MS);

      if (reducedMotion) {
        if (kind === "reset") patch({ nodeOverrides: {} });
        else patch({ nodeOverrides: target });
        return;
      }

      const t0 = performance.now();
      const token = animRef.current.token;
      const step = (now: number) => {
        if (token.canceled) return;
        const t = Math.min(1, (now - t0) / BEAUTIFY_MS);
        const e = easeInOutQuad(t);
        const frame: Record<string, Override> = {};
        for (const slug of allSlugs) {
          const a = from[slug];
          const b = target[slug] ?? basePos[slug] ?? a;
          frame[slug] = {
            x: a.x + (b.x - a.x) * e,
            y: a.y + (b.y - a.y) * e,
          };
        }
        patch({ nodeOverrides: frame });
        if (t < 1) {
          animRef.current.id = window.requestAnimationFrame(step);
        } else {
          animRef.current.id = null;
          if (kind === "reset") patch({ nodeOverrides: {} });
        }
      };
      animRef.current.id = window.requestAnimationFrame(step);
    },
    [projects, edges, placedBase, nodeOverrides, cancelAnim, clearToast, reducedMotion, patch],
  );

  // -----------------------------------------------------------------------
  // Wheel (M-FE-06: addEventListener {passive:false})
  //
  // PERF v1.2.0 (2026-04-26): rAF-throttle. Trackpad/mouse fanno fire 60-120
  // wheel/s; ogni patch trigger re-render del root + tutti i 560 SatelliteNode
  // (70 project × 8 sat). Accumuliamo deltaY tra events nello stesso frame e
  // applichiamo 1 sola patch per frame (max 60fps invece di 120fps). Ultimo
  // mouse position dell'event vince → zoom-to-cursor stabile.
  // -----------------------------------------------------------------------
  useEffect(() => {
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
      const rect = el.getBoundingClientRect();
      const ox = rect.width / 2;
      const oy = rect.height / 2;
      const wx = (pending.mx - ox - currentPan.x) / currentZoom + ox;
      const wy = (pending.my - oy - currentPan.y) / currentZoom + oy;
      const newPanX = pending.mx - ox - (wx - ox) * newZoom;
      const newPanY = pending.my - oy - (wy - oy) * newZoom;
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
  }, [cancelAnim, patch]);

  // -----------------------------------------------------------------------
  // Pointer pan (commit su pointerup, M-FE-01)
  // -----------------------------------------------------------------------
  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) return;
      // Alt+drag e' gestito a livello nodo via stopPropagation.
      (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
      setDragging(true);
      setMoved(false);
      panStart.current = {
        clientX: e.clientX,
        clientY: e.clientY,
        pan: { ...panRef.current },
      };
      cancelAnim();
    },
    [cancelAnim],
  );

  // Sub-handler: drag di un singolo nodo (Alt+drag) con repulsione locale.
  const handleNodeDragMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      const dragState = nodeDragRef.current;
      if (!dragState) return;
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const ox = rect.width / 2;
      const oy = rect.height / 2;
      const currentZoom = zoomRef.current;
      const currentPan = panRef.current;
      const wx = (mx - ox - currentPan.x) / currentZoom + ox;
      const wy = (my - oy - currentPan.y) / currentZoom + oy;
      const newX = wx - dragState.offsetX;
      const newY = wy - dragState.offsetY;
      const draggedP = bySlug[dragState.slug];
      if (!draggedP) return;
      const draggedR = draggedP.r;
      const next = { ...nodeOverrides, [dragState.slug]: { x: newX, y: newY } };
      for (const other of placed) {
        if (other.slug === dragState.slug) continue;
        const ox2 = next[other.slug]?.x ?? other.x;
        const oy2 = next[other.slug]?.y ?? other.y;
        const dx = ox2 - newX;
        const dy = oy2 - newY;
        const dist = Math.hypot(dx, dy) || 0.001;
        const minDist = draggedR + other.r + DRAG_REPULSION_GAP;
        if (dist < minDist) {
          const push = minDist - dist;
          next[other.slug] = {
            x: ox2 + (dx / dist) * push,
            y: oy2 + (dy / dist) * push,
          };
        }
      }
      patch({ nodeOverrides: next });
      setMoved(true);
    },
    [bySlug, nodeOverrides, patch, placed],
  );

  // Sub-handler: drag di pan globale (commit-on-release via ref, M-FE-01).
  const handlePanMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    const start = panStart.current;
    if (!start) return;
    const dx = e.clientX - start.clientX;
    const dy = e.clientY - start.clientY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) setMoved(true);
    const nextPan = {
      x: start.pan.x + dx,
      y: start.pan.y + dy,
    };
    panRef.current = nextPan;
    if (wrapperRef.current) {
      wrapperRef.current.style.transform = `translate(${nextPan.x}px, ${nextPan.y}px) scale(${zoomRef.current})`;
    }
  }, []);

  const onPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (nodeDragRef.current) {
        handleNodeDragMove(e);
        return;
      }
      if (!dragging) return;
      handlePanMove(e);
    },
    [dragging, handleNodeDragMove, handlePanMove],
  );

  const onPointerUp = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      try {
        (e.currentTarget as HTMLDivElement).releasePointerCapture(e.pointerId);
      } catch {
        /* pointer gia' rilasciato */
      }
      if (dragging && !nodeDragRef.current) {
        // Commit finale del pan al termine del drag.
        patch({ pan: panRef.current });
        // Guard dblclick: se il delta cumulato del drag e' sotto 3px, resetta
        // moved=false cosi' il click successivo non viene soppresso da onClick.
        // Serve per dblclick consecutivi su micro-jitter del mouse.
        const start = panStart.current;
        if (start) {
          const dx = panRef.current.x - start.pan.x;
          const dy = panRef.current.y - start.pan.y;
          if (Math.hypot(dx, dy) < 3) {
            setMoved(false);
          }
        }
      }
      setDragging(false);
      nodeDragRef.current = null;
    },
    [dragging, patch],
  );

  // Alt+drag node start.
  const startNodeDrag = useCallback(
    (e: ReactPointerEvent<HTMLElement>, p: PlacedNode): boolean => {
      if (!e.altKey) return false;
      e.stopPropagation();
      e.preventDefault();
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect) return false;
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const ox = rect.width / 2;
      const oy = rect.height / 2;
      const wx = (mx - ox - panRef.current.x) / zoomRef.current + ox;
      const wy = (my - oy - panRef.current.y) / zoomRef.current + oy;
      nodeDragRef.current = {
        slug: p.slug,
        offsetX: wx - p.x,
        offsetY: wy - p.y,
      };
      setMoved(true);
      return true;
    },
    [],
  );

  // -----------------------------------------------------------------------
  // Click canvas vuoto → clears selection (U-02)
  // -----------------------------------------------------------------------
  const onContainerClick = useCallback(
    (e: ReactMouseEvent<HTMLDivElement>) => {
      if (moved || dragging) return;
      if (e.target !== e.currentTarget) return;
      onSelect(null);
      onSelectDir(null);
    },
    [dragging, moved, onSelect, onSelectDir],
  );

  // -----------------------------------------------------------------------
  // Handler HUD
  // -----------------------------------------------------------------------
  const resetToUniverse = useCallback(() => {
    onSelect(null);
    onSelectDir(null);
    animateView(1, { x: 0, y: 0 }, 520);
  }, [onSelect, onSelectDir, animateView]);

  const resetToProject = useCallback(() => {
    if (!selected) return;
    onSelectDir(null);
    const p = bySlug[selected];
    if (!p) return;
    const target = Math.min(viewport.w, viewport.h) * 0.65;
    const newZoom = clampZoom(target / (p.r * 2));
    const ox = viewport.w / 2;
    const oy = viewport.h / 2;
    animateView(newZoom, { x: -(p.x - ox) * newZoom, y: -(p.y - oy) * newZoom });
  }, [selected, bySlug, viewport, onSelectDir, animateView]);

  const zoomOut = useCallback(() => {
    patch({ zoom: clampZoom(zoomRef.current * 0.8) });
  }, [patch]);
  const zoomIn = useCallback(() => {
    patch({ zoom: clampZoom(zoomRef.current * 1.25) });
  }, [patch]);
  const fit = useCallback(() => {
    cancelAnim();
    onSelect(null);
    onSelectDir(null);
    patch({ zoom: 1, pan: { x: 0, y: 0 } });
  }, [cancelAnim, onSelect, onSelectDir, patch]);

  const toggleBeautify = useCallback(() => setBeautifyOpen((v) => !v), []);
  const closeBeautify = useCallback(() => setBeautifyOpen(false), []);

  // -----------------------------------------------------------------------
  // Render satelliti (LOD) — memoizzato per nodo
  // -----------------------------------------------------------------------

  // Skeleton H-09
  if (placedBase.length === 0) {
    return (
      <div
        role="application"
        aria-label="Project knowledge graph"
        ref={containerRef}
        style={{
          position: "relative",
          width: "100%",
          height: "100%",
          background: "hsl(var(--pir-base))",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--pir-text-muted)",
          fontFamily: "var(--pir-font-mono)",
          fontSize: 11,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
        }}
      >
        loading canvas…
      </div>
    );
  }

  return (
    <div
      role="application"
      aria-label="Project knowledge graph"
      ref={containerRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onClick={onContainerClick}
      style={{
        position: "relative",
        flex: 1,
        minWidth: 0,
        minHeight: 0,
        width: "100%",
        height: "100%",
        background: "hsl(var(--pir-base))",
        cursor: dragging ? "grabbing" : "grab",
        overflow: "hidden",
        userSelect: "none",
        touchAction: "none",
      }}
    >
      {/* Dot texture — pattern cosmografico statico */}
      <svg
        width="100%"
        height="100%"
        style={{ position: "absolute", inset: 0, pointerEvents: "none", opacity: 0.07 }}
        aria-hidden="true"
      >
        <defs>
          <pattern id="cosmo-dots" width="28" height="28" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.7" fill="hsl(var(--bone-400))" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#cosmo-dots)" />
      </svg>

      {/* Wrapper pannato + zoomato — il transform e' scritto anche via ref durante drag.
       *
       * NO `will-change: transform`: forzava un compositing layer GPU cached come
       * bitmap; a zoom > 4x il browser upscale il layer cached invece di
       * re-renderizzare il SVG interno (blur). Trade-off accettato: pan/zoom
       * resta fluido tramite ref-write (mai setState durante drag), e il
       * browser ora re-renderizza vettoriale ad ogni cambio zoom. */}
      <div
        ref={wrapperRef}
        style={{
          position: "absolute",
          inset: 0,
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: `${viewport.w / 2}px ${viewport.h / 2}px`,
        }}
      >
        {showEdges && (
          <svg
            width={viewport.w}
            height={viewport.h}
            style={{ position: "absolute", inset: 0, pointerEvents: "none", overflow: "visible" }}
            aria-hidden="true"
          >
            {edges.map((e, i) => {
              const pa = bySlug[e.source];
              const pb = bySlug[e.target];
              if (!pa || !pb) return null;
              const isActive =
                activeSlug !== null &&
                (e.source === activeSlug || e.target === activeSlug);
              let op = edgeOpacity(e.weight, isActive, activeSlug !== null);
              // PR #21: search dim. Edge full opacity solo se entrambi gli
              // estremi matchano. Altrimenti dim a 0.05. searchMatches=null
              // → search inattiva, no override.
              if (searchMatches !== null) {
                const bothMatch =
                  searchMatches.has(e.source) && searchMatches.has(e.target);
                op = bothMatch ? op : 0.05;
              }
              // Log-scale su weight per separare edge weak (~1) da strong
              // (~100-160). La vecchia `pow(w, 0.7) * 0.7` + clamp [0.4, 5]
              // appiattiva i top weight in saturazione (~5px) rendendo edge
              // indistinguibili. `log1p` da' spread ~6x tra weight=1 e
              // weight=160:
              //   weight=1   → ~1.13
              //   weight=10  → ~3.18
              //   weight=100 → ~5.83
              //   weight=162 → ~6.40
              const thickness = 0.3 + Math.log1p(e.weight) * 1.2;
              return (
                <line
                  key={`${e.source}-${e.target}-${i}`}
                  x1={pa.x}
                  y1={pa.y}
                  x2={pb.x}
                  y2={pb.y}
                  stroke={EDGE_STROKE}
                  strokeOpacity={op}
                  // Stroke scala con il world (non counter-scaled): quando
                  // zoomi out i progetti rimpiccioliscono proporzionalmente e
                  // gli edge con loro. Senza /zoom, la relazione
                  // edge/project resta costante. I contorni project/satellite
                  // invece restano counter-scaled (1/zoom) perche' li vogliamo
                  // sempre netti come un bordo di UI.
                  strokeWidth={thickness}
                  strokeLinecap="round"
                />
              );
            })}
          </svg>
        )}

        {placed.map((p) => {
          // PR #21: search dim per project. searchMatches=null → no search,
          // no override. Match → highlight (searchHighlighted=true). Non-match
          // → dim (searchDimmed=true). Sono mutuamente esclusivi.
          const searchDimmed =
            searchMatches !== null && !searchMatches.has(p.slug);
          const searchHighlighted =
            searchMatches !== null && searchMatches.has(p.slug);
          return (
            <ProjectNode
              key={p.slug}
              p={p}
              zoom={zoom}
              selected={selected}
              hovered={hovered}
              activeSlug={activeSlug}
              connected={connectedSlugs.has(p.slug)}
              selectedDir={selectedDir}
              showLabels={showLabels}
              showSatellites={showSatellites}
              searchDimmed={searchDimmed}
              searchHighlighted={searchHighlighted}
              onSelect={onSelect}
              onHover={onHover}
              onSelectDir={onSelectDir}
              moved={moved}
              startNodeDrag={startNodeDrag}
              viewport={viewport}
              animateView={animateView}
            />
          );
        })}
      </div>

      {/* HUD — tutti memo, sopra il wrapper pannato */}
      <HudBreadcrumb
        selected={selected}
        selectedDirName={selectedDir?.name ?? null}
        nodeCount={projects.length}
        edgeCount={edges.length}
        onResetToUniverse={resetToUniverse}
        onResetToProject={resetToProject}
      />
      <HudSearch query={searchQuery} setQuery={onSearchQueryChange} />
      <HudFilters
        showLabels={showLabels}
        showSatellites={showSatellites}
        showEdges={showEdges}
        onToggleLabels={onToggleLabels}
        onToggleSatellites={onToggleSatellites}
        onToggleEdges={onToggleEdges}
      />
      <HudLegend kinds={KIND_LIST} />
      <HudZoom zoom={zoom} onZoomOut={zoomOut} onZoomIn={zoomIn} onFit={fit} />
      <HudShortcuts
        beautifyOpen={beautifyOpen}
        onToggleBeautify={toggleBeautify}
        onCloseBeautify={closeBeautify}
        onBeautify={runBeautify}
      />
      {beautifyToast && (
        <BeautifyToast label={beautifyToast} reducedMotion={reducedMotion} />
      )}
    </div>
  );
}

// -----------------------------------------------------------------------------
// SatelliteNode — sottocomponente per ridurre complessita' di ProjectNode
// -----------------------------------------------------------------------------

interface SatelliteNodeProps {
  projectSlug: string;
  sat: import("./layouts/satellitesFibonacci").PlacedSatellite;
  dirIdx: number;
  zoom: number;
  selectedDir: SelectedDir | null;
  viewport: Viewport;
  projectX: number;
  projectY: number;
  projectR: number;
  onSelect: (slug: string | null) => void;
  onSelectDir: (dir: SelectedDir | null) => void;
  animateView: (z: number, pan: { x: number; y: number }, duration?: number) => void;
}

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

/** Soglie color-tier per `latest_at` (epoca relativa a Date.now()):
 *   < FRESH_MS  → pir-accent (Riddim orange)
 *   < WARM_MS   → bone-300
 *   else        → bone-500/0.6 (cold)
 */
const FRESH_MS = 7 * 24 * 3600 * 1000;
const WARM_MS = 30 * 24 * 3600 * 1000;

/** Soglie LOD effRadius (sat.r * zoom):
 *   < 14    → SHALLOW: render solo `count` come testo centrale
 *   14..60  → MID:     fino a 6 dot + badge "+N" sotto
 *   >= 60   → DEEP:    fino a 12 dot grandi cliccabili + badge "+N"
 */
const LOD_MID_THRESHOLD = 14;
const LOD_DEEP_THRESHOLD = 60;

/** Color tier per dot in funzione di `latest_at`. */
function colorForItemAge(latestAt: string): string {
  const ts = Date.parse(latestAt);
  if (Number.isNaN(ts)) return "hsl(var(--bone-500) / 0.6)";
  const ageMs = Date.now() - ts;
  if (ageMs < FRESH_MS) return "hsl(var(--pir-accent))";
  if (ageMs < WARM_MS) return "hsl(var(--bone-300))";
  return "hsl(var(--bone-500) / 0.6)";
}

/** Radius tier per dot in funzione di `importance` (incoming edge degree).
 * Base unit clamp [0.7, 2.0] in unita' satellite. */
function radiusForItemImportance(importance: number, satRadius: number): number {
  const baseR = Math.min(2.4, satRadius * 0.15);
  // log1p(0)=0 → factor=0.7, log1p(10)≈2.4 → factor≈1.66, log1p(100)≈4.6 → factor=2.0
  const factor = 0.7 + Math.min(1.3, Math.log1p(importance) * 0.4);
  return baseR * factor;
}

/** Tooltip multi-line per un SatelliteItem. */
function itemTooltip(item: SatelliteItem): string {
  const cite = item.importance === 1 ? "cite" : "cites";
  return `${item.title}\n${item.latest_at}\n${item.importance} ${cite}`;
}

/** Calcolo posizione + stile per il dot all'indice `fi` lungo la golden spiral. */
function computeDotGeometry(
  fi: number,
  dotCount: number,
  satRadius: number,
  zoom: number,
  item: SatelliteItem | null,
  satLatestAt: string | null,
): {
  fx: number;
  fy: number;
  dotR: number;
  fillColor: string;
  opacity: number;
} {
  const t = fi / Math.max(1, dotCount - 1);
  const rr = Math.sqrt(t) * satRadius * 0.62;
  const ang = -Math.PI / 2 + fi * GOLDEN_ANGLE;
  const fx = Math.cos(ang) * rr;
  const fy = Math.sin(ang) * rr;
  const recency = 1 - fi / Math.max(1, dotCount - 1);

  let fillColor: string;
  if (item) {
    fillColor = colorForItemAge(item.latest_at);
  } else if (
    fi === 0 &&
    satLatestAt &&
    Date.now() - Date.parse(satLatestAt) < FRESH_MS
  ) {
    fillColor = "hsl(var(--pir-accent))";
  } else {
    fillColor = `hsl(var(--bone-300) / ${0.5 + recency * 0.3})`;
  }

  const baseDotR = item
    ? radiusForItemImportance(item.importance, satRadius)
    : Math.min(2.4, satRadius * 0.15) * (0.6 + recency * 0.4);
  const dotR = baseDotR / Math.max(1, Math.sqrt(zoom));
  const opacity = item ? 0.5 + 0.5 * recency : 0.4 + recency * 0.5;
  return { fx, fy, dotR, fillColor, opacity };
}

/** Render singolo file-dot. */
function renderSingleDot(
  fi: number,
  dotCount: number,
  satRadius: number,
  zoom: number,
  item: SatelliteItem | null,
  satLatestAt: string | null,
): ReactNode {
  const { fx, fy, dotR, fillColor, opacity } = computeDotGeometry(
    fi,
    dotCount,
    satRadius,
    zoom,
    item,
    satLatestAt,
  );
  const dotKey = item ? item.id : `dot-${fi}`;
  const tooltipNode = item ? <title>{itemTooltip(item)}</title> : null;

  // The dots used to link to /finder/, a hosted surface this product does not
  // ship: every click landed on a route that is not in the export.
  return (
    <circle key={dotKey} cx={fx} cy={fy} r={dotR} fill={fillColor} opacity={opacity}>
      {tooltipNode}
    </circle>
  );
}

/** Render dei "file-dots" interni al satellite con LOD 3-tier (v1.1.0).
 *
 * SHALLOW (effSatR < 14): renderizza solo il `count` come numero centrale —
 * dot illeggibili a queste dimensioni, il numero e' piu' informativo.
 *
 * MID (14 <= effSatR < 60): fino a 6 dot disposti lungo spirale di Fibonacci
 * (angolo aureo 137.5°), color/radius derivati da items (latest_at + importance).
 * Se gli items mancano (BE pre-v1.2.0) fallback a dot decorativi.
 *
 * DEEP (effSatR >= 60): fino a 12 dot grandi, cliccabili (apre finder URL su
 * `path`), tooltip con title/latest_at/importance. Sotto al satellite badge
 * "+N" se sat.count > maxDots.
 *
 * Porta evolutiva da reference-graph-v1-cosmo.html righe 1090-1119.
 */
function renderFileDots(sat: SatelliteNodeProps["sat"], zoom: number): ReactNode {
  const satRadius = sat.r;
  const effR = satRadius * zoom;
  const totalCount = sat.count;

  // Hysteresis sulla soglia MID: smooth crossfade fra SHALLOW (numero) e
  // MID/DEEP (dot) nell'intervallo [12, 14]. Evita flash on/off durante zoom
  // continuo ai threshold borderline (binary on/off → flicker percepito).
  const midOpacity = Math.max(
    0,
    Math.min(1, (effR - (LOD_MID_THRESHOLD - 2)) / 2),
  );
  const shallowOpacity = 1 - midOpacity;

  // SHALLOW (full): solo numero centrato, no dot.
  if (midOpacity <= 0) {
    if (totalCount < 1) return null;
    return (
      <text
        x={0}
        y={0}
        textAnchor="middle"
        dominantBaseline="central"
        fontFamily="var(--pir-font-mono)"
        fontSize={Math.max(8, satRadius / 2.4) / zoom}
        fontWeight={600}
        fill="hsl(var(--bone-100))"
      >
        {totalCount}
      </text>
    );
  }

  // MID + DEEP: dot semantici. Cap visibile per LOD.
  const visibleCap = effR >= LOD_DEEP_THRESHOLD ? 12 : 6;
  // Items reali dal BE Q2 v1.2.0; se vuoto fallback a synthetic da count
  // (mantiene retro-compatibilita' con BE pre-v1.2.0).
  const haveItems = sat.items.length > 0;
  const radiusCap = Math.max(2, Math.floor(satRadius * 1.8));
  const dotCount = Math.min(
    haveItems ? sat.items.length : totalCount,
    visibleCap,
    radiusCap,
  );
  if (dotCount < 1) return null;
  const hidden = Math.max(0, totalCount - dotCount);
  // Badge sotto il satellite: sempre se totalCount > 0 e siamo in MID/DEEP.
  // Format `+N` se hidden > 0 (overflow), `N` puro altrimenti (count visivo
  // affiancato ai dot — utile quando importance=0 rende i dot quasi invisibili
  // o quando arc-label non e' renderizzata per spazio insufficiente).
  const showCountBadge = totalCount > 0 && effR >= LOD_MID_THRESHOLD;
  const countBadgeText = hidden > 0 ? `+${hidden}` : `${totalCount}`;

  const dots: ReactNode[] = [];
  for (let fi = 0; fi < dotCount; fi++) {
    const item = haveItems ? sat.items[fi] : null;
    dots.push(
      renderSingleDot(
        fi,
        dotCount,
        satRadius,
        zoom,
        item,
        sat.latest_at,
      ),
    );
  }

  return (
    <>
      <g style={{ opacity: midOpacity }}>
        {dots}
        {showCountBadge && (
          <text
            x={0}
            y={satRadius + 8 / zoom}
            textAnchor="middle"
            fontFamily="var(--pir-font-mono)"
            fontSize={9 / zoom}
            fill="hsl(var(--bone-400))"
            opacity={0.85}
          >
            {countBadgeText}
          </text>
        )}
      </g>
      {shallowOpacity > 0 && totalCount >= 1 && (
        <text
          x={0}
          y={0}
          textAnchor="middle"
          dominantBaseline="central"
          fontFamily="var(--pir-font-mono)"
          fontSize={Math.max(8, satRadius / 2.4) / zoom}
          fontWeight={600}
          fill="hsl(var(--bone-100))"
          opacity={shallowOpacity}
        >
          {totalCount}
        </text>
      )}
    </>
  );
}

/** Render arc-label (textPath) sul rim interno superiore del satellite.
 *
 * LOD profondo: progressivamente il name del kind appare come testo curvato
 * sulla circonferenza interna. Font counter-scaled con zoom. Porta letterale
 * da reference-graph-v1-cosmo.html righe 1029-1089.
 */
function renderArcLabel(
  sat: SatelliteNodeProps["sat"],
  zoom: number,
  isRecent: boolean,
  arcId: string,
): ReactNode {
  const fontSizeScreen = 12;
  const fontSize = fontSizeScreen / zoom;
  const innerR = sat.r - fontSize * 1.0;
  if (innerR <= 2) return null;
  const avgCharW = fontSize * 0.56;
  const maxSpan = (140 * Math.PI) / 180;
  const arcLen = innerR * maxSpan;
  const maxChars = Math.max(3, Math.floor(arcLen / avgCharW));
  const labelText = sat.name.toUpperCase();
  const fitted =
    labelText.length > maxChars
      ? labelText.slice(0, maxChars - 1) + "…"
      : labelText;
  const textArcLen = fitted.length * avgCharW;
  const centerAng = -Math.PI / 2;
  const half = Math.min(maxSpan / 2, textArcLen / (innerR * 2) + 0.1);
  const tx1 = Math.cos(centerAng - half) * innerR;
  const ty1 = Math.sin(centerAng - half) * innerR;
  const tx2 = Math.cos(centerAng + half) * innerR;
  const ty2 = Math.sin(centerAng + half) * innerR;
  const maskHalf = half + 0.05;
  const mx1 = Math.cos(centerAng - maskHalf) * sat.r;
  const my1 = Math.sin(centerAng - maskHalf) * sat.r;
  const mx2 = Math.cos(centerAng + maskHalf) * sat.r;
  const my2 = Math.sin(centerAng + maskHalf) * sat.r;
  const maskStroke = isRecent
    ? "hsl(var(--bone-50))"
    : "hsl(var(--bone-400) / 0.7)";
  const textFill = isRecent
    ? "hsl(var(--pir-accent))"
    : "hsl(var(--bone-700))";
  return (
    <>
      <path
        d={`M ${mx1} ${my1} A ${sat.r} ${sat.r} 0 0 1 ${mx2} ${my2}`}
        fill="none"
        stroke={maskStroke}
        strokeWidth={(isRecent ? 2.4 : 1.8) / zoom}
        strokeLinecap="butt"
      />
      <defs>
        <path
          id={arcId}
          d={`M ${tx1} ${ty1} A ${innerR} ${innerR} 0 0 1 ${tx2} ${ty2}`}
        />
      </defs>
      <text
        fontFamily="var(--pir-font-mono)"
        fontSize={fontSize}
        fontWeight={500}
        fill={textFill}
        opacity={isRecent ? 1 : 0.9}
        letterSpacing="0.02em"
      >
        <textPath href={`#${arcId}`} startOffset="50%" textAnchor="middle">
          {fitted}
        </textPath>
      </text>
    </>
  );
}

function SatelliteNodeImpl({
  projectSlug,
  sat,
  dirIdx,
  zoom,
  selectedDir,
  viewport,
  projectX,
  projectY,
  projectR,
  onSelect,
  onSelectDir,
  animateView,
}: SatelliteNodeProps) {
  const isDirSelected =
    selectedDir?.projectSlug === projectSlug && selectedDir?.dirIdx === dirIdx;
  const isRecent = dirIdx === 0;
  const localCx = sat.x - projectX + projectR;
  const localCy = sat.y - projectY + projectR;

  // Palette bone full-fidelity (porta 1:1 dal reference):
  //   recent (idx 0) / selected → bone-50 fill + pir-accent stroke (Riddim orange)
  //   older                     → bone-400 @ 0.7 fill + bone-500 stroke
  const fill =
    isDirSelected || isRecent
      ? "hsl(var(--bone-50))"
      : "hsl(var(--bone-400) / 0.7)";
  const stroke =
    isDirSelected || isRecent
      ? "hsl(var(--pir-accent))"
      : "hsl(var(--bone-500) / 0.8)";

  // LOD thresholds — pixel effettivi (sat.r scalato a zoom). `effSatR` non
  // piu' calcolato qui perche' il dispatch LOD vive dentro renderFileDots.
  // File-dots: dispatched dentro renderFileDots con LOD 3-tier (v1.1.0):
  //   SHALLOW (effSatR < 14)  → count come testo centrale
  //   MID     (14..60)        → fino a 6 dot decorativi/items
  //   DEEP    (>= 60)         → fino a 12 dot cliccabili (con tooltip + finder)
  // Gate solo zoom-aware (effSatR): il cap raw `sat.r >= 8` bloccava i
  // satelliti Fibonacci di indice 5+ (radius normalizzato piccolo) anche
  // dopo zoom in. Ora basta che il satellite sia >= 8px on-screen.
  const effSatR = sat.r * zoom;
  const showFiles = effSatR >= 8 && sat.count > 0;
  // Arc-label: visibile quando il satellite e' abbastanza grande on-screen
  // per ospitare il testo leggibile (effSatR >= 30 — alzato da 16 perche' a
  // 16 il testo era oversized e veniva troncato). Hysteresis con opacity
  // smooth da 27 a 30 per evitare flash on/off durante zoom continuo.
  const arcLabelOpacity = Math.max(0, Math.min(1, (effSatR - 27) / 3));
  const showArcLabel = arcLabelOpacity > 0;
  const arcId = `arc-${projectSlug}-${dirIdx}`;
  // Fallback fib number label — solo se NON arc-label fully on e NON file-dots.
  // Usa la soglia full-on (>= 30) per evitare doppia label in zona transizione.
  const showFibLabel =
    effSatR >= 8 && projectR >= 40 && !showFiles && arcLabelOpacity < 1;
  const fibTextFill = isRecent
    ? "hsl(var(--pir-accent))"
    : "hsl(var(--bone-800))";

  const select = (e: ReactMouseEvent<SVGGElement>) => {
    e.stopPropagation();
    onSelect(projectSlug);
    onSelectDir({
      projectSlug,
      dirIdx,
      kind: sat.kind,
      name: sat.name,
    });
  };

  const zoomFit = (e: ReactMouseEvent<SVGGElement>) => {
    e.stopPropagation();
    const target = Math.min(viewport.w, viewport.h) * 0.55;
    const newZoom = clampZoom(target / (sat.r * 2));
    const ox = viewport.w / 2;
    const oy = viewport.h / 2;
    onSelect(projectSlug);
    onSelectDir({
      projectSlug,
      dirIdx,
      kind: sat.kind,
      name: sat.name,
    });
    animateView(newZoom, {
      x: -(sat.x - ox) * newZoom,
      y: -(sat.y - oy) * newZoom,
    });
  };

  return (
    <g
      transform={`translate(${localCx}, ${localCy})`}
      style={{ cursor: "pointer" }}
      onPointerDown={(e) => e.stopPropagation()}
      onClick={select}
      onDoubleClick={zoomFit}
    >
      <circle
        r={sat.r}
        fill={fill}
        stroke={stroke}
        strokeWidth={satStrokeWidth(isDirSelected, isRecent) / zoom}
        strokeOpacity={isDirSelected || isRecent ? 1 : 0.75}
      />
      {showArcLabel && (
        <g style={{ opacity: arcLabelOpacity }}>
          {renderArcLabel(sat, zoom, isRecent, arcId)}
        </g>
      )}
      {showFibLabel && (
        <text
          x="0"
          y={5 / zoom}
          textAnchor="middle"
          fontFamily="var(--pir-font-mono)"
          fontSize={Math.min(sat.r * 0.8, 14) / zoom}
          fontWeight={600}
          fill={fibTextFill}
          opacity={isRecent ? 1 : 0.8}
        >
          {sat.fibValue}
        </text>
      )}
      {showFiles && renderFileDots(sat, zoom)}
    </g>
  );
}

/** Threshold LOD/hysteresis (effective sat radius = sat.r * zoom):
 *   8     → showFiles gate
 *   12,14 → SHALLOW↔MID hysteresis (count number ↔ dot)
 *   27,30 → arc-label crossfade
 *   60    → MID↔DEEP (clickable dots)
 */
const SAT_LOD_THRESHOLDS: readonly number[] = [8, 12, 14, 27, 30, 60];

function isSatPropsIdentityEqual(
  prev: SatelliteNodeProps,
  next: SatelliteNodeProps,
): boolean {
  return (
    prev.sat === next.sat &&
    prev.dirIdx === next.dirIdx &&
    prev.projectSlug === next.projectSlug &&
    prev.projectR === next.projectR &&
    prev.projectX === next.projectX &&
    prev.projectY === next.projectY &&
    prev.onSelect === next.onSelect &&
    prev.onSelectDir === next.onSelectDir &&
    prev.animateView === next.animateView &&
    prev.viewport.w === next.viewport.w &&
    prev.viewport.h === next.viewport.h
  );
}

function isSatDirSelectionEqual(
  prev: SatelliteNodeProps,
  next: SatelliteNodeProps,
): boolean {
  const prevSel =
    prev.selectedDir?.projectSlug === prev.projectSlug &&
    prev.selectedDir?.dirIdx === prev.dirIdx;
  const nextSel =
    next.selectedDir?.projectSlug === next.projectSlug &&
    next.selectedDir?.dirIdx === next.dirIdx;
  return prevSel === nextSel;
}

/** True se delta zoom non attraversa threshold LOD ne' hysteresis zone, e
 * delta relativo < 1% (re-render visivamente impercettibile). */
function isSatZoomDeltaSkippable(
  satR: number,
  prevZoom: number,
  nextZoom: number,
): boolean {
  if (prevZoom === nextZoom) return true;
  const prevEff = satR * prevZoom;
  const nextEff = satR * nextZoom;
  for (const t of SAT_LOD_THRESHOLDS) {
    if ((prevEff < t) !== (nextEff < t)) return false;
  }
  // Hysteresis zone: opacity interpolata → re-render anche dentro.
  const inMidZone =
    (prevEff >= 12 && prevEff <= 14) || (nextEff >= 12 && nextEff <= 14);
  const inArcZone =
    (prevEff >= 27 && prevEff <= 30) || (nextEff >= 27 && nextEff <= 30);
  if (inMidZone || inArcZone) return false;
  const avgZoom = (prevZoom + nextZoom) / 2;
  return Math.abs(prevZoom - nextZoom) / avgZoom < 0.01;
}

/** Custom comparator per SatelliteNode (PERF v1.2.0).
 *
 * Problema: shallow-memo default re-rendererebbe ogni satellite ad ogni cambio
 * di `zoom` (continuo durante wheel). 70 project × 8 sat = 560 sat → 33k+
 * render/s a zoom continuo. Il transform `translate` viene applicato dal
 * wrapper `<g>` esterno (project SVG): il satellite scala correttamente anche
 * senza re-render del componente.
 *
 * Strategia: re-render solo se attraversa threshold LOD/hysteresis o se delta
 * zoom > 1%. Vedi `isSatZoomDeltaSkippable`.
 */
function satelliteNodePropsEqual(
  prev: SatelliteNodeProps,
  next: SatelliteNodeProps,
): boolean {
  if (!isSatPropsIdentityEqual(prev, next)) return false;
  if (!isSatDirSelectionEqual(prev, next)) return false;
  return isSatZoomDeltaSkippable(next.sat.r, prev.zoom, next.zoom);
}

const SatelliteNode = memo(SatelliteNodeImpl, satelliteNodePropsEqual);

// -----------------------------------------------------------------------------
// ProjectNode — memoizzato, riceve solo props plain
// -----------------------------------------------------------------------------

interface ProjectNodeProps {
  p: PlacedNode;
  zoom: number;
  selected: string | null;
  hovered: string | null;
  activeSlug: string | null;
  connected: boolean;
  selectedDir: SelectedDir | null;
  showLabels: boolean;
  showSatellites: boolean;
  /** Search dim — true se search attiva e questo project NON matcha. */
  searchDimmed: boolean;
  /** Search highlight — true se search attiva e questo project matcha. */
  searchHighlighted: boolean;
  viewport: Viewport;
  moved: boolean;
  onSelect: (slug: string | null) => void;
  onHover: (slug: string | null) => void;
  onSelectDir: (dir: SelectedDir | null) => void;
  startNodeDrag: (e: ReactPointerEvent<HTMLElement>, p: PlacedNode) => boolean;
  animateView: (z: number, pan: { x: number; y: number }, duration?: number) => void;
}

function ProjectNodeImpl({
  p,
  zoom,
  selected,
  hovered,
  activeSlug,
  connected,
  selectedDir,
  showLabels,
  showSatellites,
  searchDimmed,
  searchHighlighted,
  viewport,
  moved,
  onSelect,
  onHover,
  onSelectDir,
  startNodeDrag,
  animateView,
}: ProjectNodeProps) {
  const isSelected = selected === p.slug;
  const isHovered = hovered === p.slug;
  // searchHighlighted promuove il nodo a "focus-like" (accent ring + opacity 1)
  // anche senza select/hover. Il dim search ha priorita' assoluta sul dim
  // hover/select-connected esistente (search e' un secondo livello di filter).
  const isFocus = isSelected || isHovered || searchHighlighted;
  const isDim = activeSlug !== null && !isFocus && !connected;
  let opacity: number;
  if (searchDimmed) opacity = 0.15;
  else if (isDim) opacity = 0.24;
  else opacity = 1;

  // LOD satelliti — soglie pixel effettivo. Soglia abbassata a 24 per mostrare
  // almeno 2 satelliti sul project-medio a zoom 1 (era 48 → satelliti invisibili
  // fino a zoom ~1.5×, fallimento P0 del canvas).
  const effR = p.r * zoom;
  const satsVisible = showSatellites && effR >= 24 && p.satellites.length > 0;
  const maxSats = maxSatellitesFor(effR);
  const satelliteSummaries = p.satellites.slice(
    0,
    Math.min(p.satellites.length, maxSats),
  );
  const sats = satsVisible ? layoutSatellitesFib(p, satelliteSummaries) : [];

  const onClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
    if (moved) return;
    onSelect(p.slug);
    onSelectDir(null);
  };
  const onDoubleClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    e.stopPropagation();
    onSelect(p.slug);
    onSelectDir(null);
    const target = Math.min(viewport.w, viewport.h) * 0.65;
    const newZoom = clampZoom(target / (p.r * 2));
    const ox = viewport.w / 2;
    const oy = viewport.h / 2;
    animateView(newZoom, {
      x: -(p.x - ox) * newZoom,
      y: -(p.y - oy) * newZoom,
    });
  };

  return (
    <div
      onPointerDown={(e) => {
        // stopPropagation SEMPRE: impedisce al container di entrare in
        // pan-drag mode quando cliccando/trascinando un nodo (sbloccando
        // dblclick consecutivi e prevenendo pan sul nodo). Alt-drag gia'
        // fa stopPropagation dentro startNodeDrag, ma va fatto anche per
        // click normali per isolare il nodo dal container.
        e.stopPropagation();
        startNodeDrag(e, p);
      }}
      onMouseEnter={() => onHover(p.slug)}
      onMouseLeave={() => onHover(null)}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      title={`${p.slug} · degree ${p.degree.toFixed(1)}`}
      style={{
        position: "absolute",
        left: p.x - p.r,
        top: p.y - p.r,
        width: p.r * 2,
        height: p.r * 2,
        opacity,
        transition: "opacity 160ms",
        cursor: "pointer",
      }}
    >
      <svg width={p.r * 2} height={p.r * 2} style={{ overflow: "visible" }}>
        <circle
          cx={p.r}
          cy={p.r}
          r={p.r - 0.5}
          fill={projectFillColor(p, isFocus)}
          stroke={isFocus ? "hsl(var(--pir-accent))" : "hsl(var(--bone-300))"}
          strokeWidth={projectStrokeWidth(isSelected, isHovered) / zoom}
          strokeOpacity={p.color || isFocus ? 1 : 0.6}
        />
        {isFocus && (
          <circle
            cx={p.r}
            cy={p.r}
            r={p.r + 4 / zoom}
            fill="none"
            stroke="hsl(var(--pir-accent))"
            strokeWidth={1 / zoom}
            strokeOpacity={0.35}
          />
        )}

        {sats.map((s, i) => (
          <SatelliteNode
            key={`${p.slug}-sat-${i}`}
            projectSlug={p.slug}
            sat={s}
            dirIdx={i}
            zoom={zoom}
            selectedDir={selectedDir}
            viewport={viewport}
            projectX={p.x}
            projectY={p.y}
            projectR={p.r}
            onSelect={onSelect}
            onSelectDir={onSelectDir}
            animateView={animateView}
          />
        ))}
        {satsVisible && p.satellites.length > sats.length && p.r >= 36 && (
          <g transform={`translate(${p.r * 1.55}, ${p.r * 1.55})`}>
            <text
              x="0"
              y="0"
              textAnchor="middle"
              fontFamily="var(--pir-font-mono)"
              fontSize={9}
              fontWeight={600}
              fill="hsl(var(--bone-600))"
              opacity={0.85}
              letterSpacing="0.05em"
            >
              +{p.satellites.length - sats.length}
            </text>
          </g>
        )}
      </svg>
      <ProjectLabel p={p} zoom={zoom} isFocus={isFocus} showLabels={showLabels} />
    </div>
  );
}

/** Threshold LOD ProjectNode (effective project radius = p.r * zoom):
 *   24       → satsVisible gate
 *   48,72,120 → maxSatellitesFor tier (2/3/5/8 sat)
 *   36       → +N badge gate (raw p.r >= 36)
 *   40       → fib label / stroke tier
 *   14       → label visibility (raw p.r < 14)
 */
const PROJECT_LOD_THRESHOLDS: readonly number[] = [24, 48, 72, 120];

function isProjectNodeIdentityEqual(
  prev: ProjectNodeProps,
  next: ProjectNodeProps,
): boolean {
  return (
    prev.p === next.p &&
    prev.connected === next.connected &&
    prev.showLabels === next.showLabels &&
    prev.showSatellites === next.showSatellites &&
    prev.searchDimmed === next.searchDimmed &&
    prev.searchHighlighted === next.searchHighlighted &&
    prev.moved === next.moved &&
    prev.viewport.w === next.viewport.w &&
    prev.viewport.h === next.viewport.h &&
    prev.onSelect === next.onSelect &&
    prev.onHover === next.onHover &&
    prev.onSelectDir === next.onSelectDir &&
    prev.startNodeDrag === next.startNodeDrag &&
    prev.animateView === next.animateView
  );
}

function isProjectNodeFocusEqual(
  prev: ProjectNodeProps,
  next: ProjectNodeProps,
): boolean {
  // Focus state per QUESTO project (selected/hovered).
  const prevSel = prev.selected === prev.p.slug;
  const nextSel = next.selected === next.p.slug;
  if (prevSel !== nextSel) return false;
  const prevHov = prev.hovered === prev.p.slug;
  const nextHov = next.hovered === next.p.slug;
  if (prevHov !== nextHov) return false;
  // activeSlug presence: cambia opacity dim sui non-focus/non-connected.
  const prevHasActive = (prev.hovered ?? prev.selected) !== null;
  const nextHasActive = (next.hovered ?? next.selected) !== null;
  return prevHasActive === nextHasActive;
}

function isProjectSelectedDirEqual(
  prev: ProjectNodeProps,
  next: ProjectNodeProps,
): boolean {
  // selectedDir cambia render solo se relativo a QUESTO project.
  const prevDir =
    prev.selectedDir?.projectSlug === prev.p.slug ? prev.selectedDir : null;
  const nextDir =
    next.selectedDir?.projectSlug === next.p.slug ? next.selectedDir : null;
  if (prevDir === nextDir) return true;
  if (prevDir === null || nextDir === null) return false;
  return prevDir.dirIdx === nextDir.dirIdx;
}

/** True se delta zoom non attraversa threshold LOD project ne' threshold sat
 * derivati (i sat scale con `p.r * 0.92` × fib-fraction; usiamo `p.r * 0.92`
 * come upper bound conservativo per propagare le soglie sat al parent —
 * altrimenti ProjectNode skip impedirebbe i figli SatelliteNode di vedere il
 * nuovo zoom). Delta < 1%. */
function isProjectZoomDeltaSkippable(
  pR: number,
  prevZoom: number,
  nextZoom: number,
): boolean {
  if (prevZoom === nextZoom) return true;
  // Project thresholds (eff = p.r * zoom).
  const prevEffP = pR * prevZoom;
  const nextEffP = pR * nextZoom;
  for (const t of PROJECT_LOD_THRESHOLDS) {
    if ((prevEffP < t) !== (nextEffP < t)) return false;
  }
  // Satellite thresholds — usiamo `p.r * 0.92` come radius sat upper bound:
  // qualsiasi sat-r reale e' <= di questo, quindi se prevEff/nextEff (su
  // questo bound) attraversano una soglia, ALMENO un sat reale potrebbe
  // attraversarla → non skippare. Conservative: piu' re-render del minimo
  // necessario, ma garantisce LOD fresco sui sat.
  const prevEffSatBound = pR * 0.92 * prevZoom;
  const nextEffSatBound = pR * 0.92 * nextZoom;
  for (const t of SAT_LOD_THRESHOLDS) {
    if ((prevEffSatBound < t) !== (nextEffSatBound < t)) return false;
  }
  if (
    (prevEffSatBound >= 12 && prevEffSatBound <= 14) ||
    (nextEffSatBound >= 12 && nextEffSatBound <= 14) ||
    (prevEffSatBound >= 27 && prevEffSatBound <= 30) ||
    (nextEffSatBound >= 27 && nextEffSatBound <= 30)
  ) {
    return false;
  }
  const avgZoom = (prevZoom + nextZoom) / 2;
  return Math.abs(prevZoom - nextZoom) / avgZoom < 0.01;
}

/** Custom comparator per ProjectNode (PERF v1.2.0).
 *
 * Stesso pattern di SatelliteNode: stroke counter-scale (`/zoom`) e label
 * scaling sono CSS-driven, non richiedono re-render React. Re-render solo
 * a transizioni LOD discrete (24/48/72/120 → tier maxSatellitesFor + sat
 * visibility) o focus/selected change.
 */
function projectNodePropsEqual(
  prev: ProjectNodeProps,
  next: ProjectNodeProps,
): boolean {
  if (!isProjectNodeIdentityEqual(prev, next)) return false;
  if (!isProjectNodeFocusEqual(prev, next)) return false;
  if (!isProjectSelectedDirEqual(prev, next)) return false;
  // p stable per identity check sopra → next.p.r equivale a prev.p.r.
  return isProjectZoomDeltaSkippable(next.p.r, prev.zoom, next.zoom);
}

const ProjectNode = memo(ProjectNodeImpl, projectNodePropsEqual);

export const GraphCanvas = memo(GraphCanvasImpl);
