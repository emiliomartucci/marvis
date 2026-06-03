// v1.0.0 - 2026-05-17 - Engine condiviso Cosmo+Codex: hook pan/zoom riusabile.
//
// Estratto da cosmo/GraphCanvas.tsx:543-592 (commit `e82ef4e` rAF-throttle +
// math zoom-to-cursor). Distribuisce a Codex il fix cruciale: zoom su
// posizione mouse, non centro viewport.
//
// Tre cose critiche fatte insieme — toglierne una sola fa crashare il behavior:
//  1. addEventListener("wheel", h, {passive: false}) → preventDefault funziona
//  2. rAF-throttle accumulato → max 60fps (33k re-render/s a zoom continuo
//     altrimenti se i consumer hanno componenti memoizzati su zoom)
//  3. math zoom-to-cursor → world coord sotto cursore resta fisso post-zoom
//
// Pan via pointer events con commit-on-release (no re-render per frame durante
// drag) — il consumer riceve zoom/pan via callback in stato React esterno.
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

import { clamp } from "./lodHysteresis";

/** @public */
export interface PanZoomState {
  readonly zoom: number;
  readonly pan: { readonly x: number; readonly y: number };
}

/** @public */
export interface UsePanZoomOptions {
  readonly minZoom: number;
  readonly maxZoom: number;
  /** Default 0.7. Reset via `reset()`. */
  readonly initialZoom?: number;
  /** Default { x: 0, y: 0 }. */
  readonly initialPan?: { readonly x: number; readonly y: number };
  /** Default 0.0015 → wheel-tick factor ≈1.06. Aumenta per zoom piu reattivo. */
  readonly wheelSensitivity?: number;
  /** Soglia per skip patch (default 1e-4). */
  readonly epsilon?: number;
}

/** @public */
export interface UsePanZoomReturn extends PanZoomState {
  /** Ref da attaccare al container HTMLDivElement che riceve wheel/pan. */
  readonly containerRef: React.RefObject<HTMLDivElement | null>;
  /** Handler onPointerDown da spreadare sul container. */
  readonly onPointerDown: (e: ReactPointerEvent<HTMLDivElement>) => void;
  /** Handler onPointerMove da spreadare sul container. */
  readonly onPointerMove: (e: ReactPointerEvent<HTMLDivElement>) => void;
  /** Handler onPointerUp / onPointerCancel da spreadare sul container. */
  readonly onPointerUp: (e: ReactPointerEvent<HTMLDivElement>) => void;
  /** Reset a initial zoom/pan. */
  readonly reset: () => void;
  /** Set programmatic (es. "fit to view" calcolato dal consumer). */
  readonly setView: (next: PanZoomState) => void;
  /** True mentre il pointer e premuto + draggando. */
  readonly isDragging: boolean;
}

/**
 * Hook pan/zoom con zoom-at-cursor + rAF-throttle.
 *
 * Uso:
 * ```tsx
 * const { containerRef, zoom, pan, onPointerDown, onPointerMove, onPointerUp } =
 *   usePanZoom({ minZoom: 0.25, maxZoom: 4, initialZoom: 0.7 });
 * return (
 *   <div ref={containerRef} onPointerDown={onPointerDown} ...>
 *     <div style={{ transform: `translate(${pan.x * zoom}px, ${pan.y * zoom}px) scale(${zoom})` }}>
 *       ...
 *     </div>
 *   </div>
 * );
 * ```
 * @public
 */
export function usePanZoom(opts: UsePanZoomOptions): UsePanZoomReturn {
  const {
    minZoom,
    maxZoom,
    initialZoom = 0.7,
    initialPan = { x: 0, y: 0 },
    wheelSensitivity = 0.0015,
    epsilon = 1e-4,
  } = opts;

  const containerRef = useRef<HTMLDivElement | null>(null);
  const [zoom, setZoom] = useState(initialZoom);
  const [pan, setPan] = useState(initialPan);
  const [isDragging, setIsDragging] = useState(false);

  // Refs per accesso sincrono nel listener wheel (e nel pointer move) senza
  // dipendenze stale: il setState e' async, rAF flush e' fuori sync con i
  // pointer events.
  const zoomRef = useRef(zoom);
  const panRef = useRef(pan);
  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);
  useEffect(() => {
    panRef.current = pan;
  }, [pan]);

  // Wheel: non-passive listener + rAF-throttle + zoom-at-cursor.
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

      const currentZoom = zoomRef.current;
      const currentPan = panRef.current;
      const delta = -accumulated * wheelSensitivity;
      const factor = 1 + delta;
      const newZoom = clamp(currentZoom * factor, minZoom, maxZoom);
      if (Math.abs(newZoom - currentZoom) < epsilon) return;

      if (!pending.hasMouse) {
        setZoom(newZoom);
        return;
      }
      const rect = el.getBoundingClientRect();
      const ox = rect.width / 2;
      const oy = rect.height / 2;
      // World coord del punto sotto al cursore PRIMA del cambio zoom:
      const wx = (pending.mx - ox - currentPan.x) / currentZoom + ox;
      const wy = (pending.my - oy - currentPan.y) / currentZoom + oy;
      // Nuovo pan tale che lo stesso (wx,wy) resti sotto (mx,my):
      const newPanX = pending.mx - ox - (wx - ox) * newZoom;
      const newPanY = pending.my - oy - (wy - oy) * newZoom;
      setZoom(newZoom);
      setPan({ x: newPanX, y: newPanY });
    };

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = el.getBoundingClientRect();
      pending.deltaY += e.deltaY;
      pending.mx = e.clientX - rect.left;
      pending.my = e.clientY - rect.top;
      pending.hasMouse = true;
      if (rafId === null) {
        rafId = window.requestAnimationFrame(flush);
      }
    };

    // capture:true → cattura il wheel PRIMA che eventuali child eat l'event.
    // passive:false → preventDefault funziona (default React onWheel JSX
    // registra passive, vedi PR1 commit cb8b3a9).
    el.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => {
      el.removeEventListener("wheel", onWheel, { capture: true } as EventListenerOptions);
      if (rafId !== null) window.cancelAnimationFrame(rafId);
    };
  }, [minZoom, maxZoom, wheelSensitivity, epsilon]);

  // Pan via pointer events. Commit on every move (consumer puo essere ottimizzato).
  const dragStartRef = useRef<{
    clientX: number;
    clientY: number;
    panX: number;
    panY: number;
  } | null>(null);

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) return;
      (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
      dragStartRef.current = {
        clientX: e.clientX,
        clientY: e.clientY,
        panX: panRef.current.x,
        panY: panRef.current.y,
      };
      setIsDragging(true);
    },
    [],
  );

  const onPointerMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStartRef.current;
    if (!start) return;
    const currentZoom = zoomRef.current;
    setPan({
      x: start.panX + (e.clientX - start.clientX) / currentZoom,
      y: start.panY + (e.clientY - start.clientY) / currentZoom,
    });
  }, []);

  const onPointerUp = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (dragStartRef.current) {
      dragStartRef.current = null;
      setIsDragging(false);
    }
    if ((e.currentTarget as HTMLDivElement).hasPointerCapture(e.pointerId)) {
      (e.currentTarget as HTMLDivElement).releasePointerCapture(e.pointerId);
    }
  }, []);

  const reset = useCallback(() => {
    setZoom(initialZoom);
    setPan(initialPan);
  }, [initialZoom, initialPan]);

  const setView = useCallback((next: PanZoomState) => {
    setZoom(clamp(next.zoom, minZoom, maxZoom));
    setPan(next.pan);
  }, [minZoom, maxZoom]);

  return {
    containerRef,
    zoom,
    pan,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    reset,
    setView,
    isDragging,
  };
}
