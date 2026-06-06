// v1.0.0 - 2026-04-22 - L5 orbital-blob loader (theme-v2 footer strip)
"use client";

import type { CSSProperties } from "react";

interface L5LoaderProps {
  size?: number;
}

/**
 * L5 — 3-orbit moon loader fused via gooey SVG filter.
 *
 * Source of truth: /data/projects/marvisx/input/20260422-115024_loader-L5-final.html
 * Usage: footer strip in TerminalPanel v2 (18x18 default). Scalable via `size`.
 *
 * Colors bind to CSS vars that resolve per theme:
 *  - core + inner moon: hsl(var(--pir-accent))  (orange)
 *  - middle moon: hsl(var(--pir-secondary-bright)) fallback hsl(var(--pir-success)) (green)
 *  - outer moon: hsl(var(--pir-bone)) in dark, terracotta override in .light.theme-v2
 *
 * Respects prefers-reduced-motion: reduce → animations paused in globals.css.
 */
export function L5Loader({ size = 18 }: L5LoaderProps) {
  return (
    <>
      {/* Shared SVG filter — rendered once per component mount. Multiple L5Loader
          instances on the page all reference the same `pir-l5-goo` id; duplicates
          are harmless because SVG filter refs resolve by id at paint time. */}
      <svg
        width="0"
        height="0"
        style={{ position: "absolute", pointerEvents: "none" }}
        aria-hidden
      >
        <defs>
          <filter id="pir-l5-goo">
            <feGaussianBlur in="SourceGraphic" stdDeviation="1.4" result="blur" />
            <feColorMatrix
              in="blur"
              mode="matrix"
              values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 18 -7"
              result="goo"
            />
            <feComposite in="SourceGraphic" in2="goo" operator="atop" />
          </filter>
        </defs>
      </svg>
      <span
        className="pir-l5"
        style={{ "--pir-l5-sz": `${size}px` } as CSSProperties}
        aria-hidden
      >
        <span className="pir-l5-wrap">
          <span className="pir-l5-core" />
          <span className="pir-l5-orbit pir-l5-orbit-a">
            <span className="pir-l5-p" />
          </span>
          <span className="pir-l5-orbit pir-l5-orbit-b">
            <span className="pir-l5-p" />
          </span>
          <span className="pir-l5-orbit pir-l5-orbit-c">
            <span className="pir-l5-p" />
          </span>
        </span>
      </span>
    </>
  );
}
