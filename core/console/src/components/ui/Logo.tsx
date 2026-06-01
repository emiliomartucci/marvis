"use client";

import { useDesignV2 } from "@/lib/useDesignV2";

type LogoSize = "sm" | "md" | "lg";

interface LogoProps {
  size?: LogoSize;
}

// Size tables — declarative, avoid nested ternaries flagged by SonarJS.
const V1_DIMS: Record<LogoSize, { w: number; h: number }> = {
  sm: { w: 120, h: 30 },
  md: { w: 160, h: 40 },
  lg: { w: 200, h: 50 },
};

const V2_HEIGHT: Record<LogoSize, number> = {
  sm: 28,
  md: 36,
  lg: 48,
};

// ==== LogoV1 ====================================================
// Original chevrons + wordmark. Kept exactly as-is visually so v1 default
// matches pre-PR rendering. Migration plan: flip default after Phase 3.
function LogoV1({ size = "md" }: LogoProps) {
  const { w, h } = V1_DIMS[size];
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 160 40"
      width={w}
      height={h}
      role="img"
      aria-label="MarvisX"
    >
      {/* 3 chevrons — forward motion, layered opacity */}
      <g
        stroke="#3e7eff"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      >
        <path d="M 8 12 L 16 20 L 8 28" opacity="0.3" />
        <path d="M 16 12 L 24 20 L 16 28" opacity="0.6" />
        <path d="M 24 12 L 32 20 L 24 28" />
      </g>
      {/* Wordmark: "Marvis" adaptive + "X" in indigo */}
      <text
        x="42"
        y="25"
        fontFamily="system-ui, -apple-system, sans-serif"
        fontWeight="600"
        fontSize="16"
        fill="currentColor"
      >
        Marvis
      </text>
      <text
        x="95"
        y="25"
        fontFamily="system-ui, -apple-system, sans-serif"
        fontWeight="700"
        fontSize="18"
        fill="#3e7eff"
      >
        X
      </text>
    </svg>
  );
}

// ==== LogoV2 ====================================================
// TE industrial lockup — handoff 2026-04-22. Monitor device-mark + wordmark
// "marvisx" with Riddim orange "x" and JetBrains Mono tagline.
// Source SVG: /data/projects/marvisx/design/project/assets/logo-lockup.svg
// Mirror: console/public/logo-lockup.svg
function LogoV2({ size = "md" }: LogoProps) {
  // Lockup aspect ratio 320x72 (~4.44:1). Height follows the shell nav scale.
  const h = V2_HEIGHT[size];
  const w = Math.round((h * 320) / 72);
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 320 72"
      width={w}
      height={h}
      fill="none"
      role="img"
      aria-label="MarvisX"
    >
      {/* Monitor device-mark — anthracite base, Riddim orange accents */}
      <g transform="translate(8, 7) scale(0.437)">
        <line x1="55" y1="4" x2="55" y2="14" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="55" cy="4" r="3" fill="#F6581C" />
        <rect
          x="16"
          y="14"
          width="78"
          height="44"
          rx="6"
          fill="#F2ECDF"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <rect x="26" y="22" width="58" height="20" rx="2" fill="#1C5C42" />
        <rect x="34" y="29" width="6" height="6" fill="#F6581C" />
        <rect x="70" y="29" width="6" height="6" fill="#F6581C" />
        <rect x="36" y="48" width="38" height="3" rx="1" fill="#F6581C" />
        <rect x="48" y="58" width="14" height="6" fill="currentColor" />
        <rect
          x="20"
          y="64"
          width="70"
          height="48"
          rx="4"
          fill="#F2ECDF"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <circle cx="32" cy="76" r="2.5" fill="#2E9668" />
        <circle cx="42" cy="76" r="2.5" fill="#F6581C" />
        <rect
          x="32"
          y="112"
          width="12"
          height="10"
          rx="1"
          fill="#F2ECDF"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <rect
          x="66"
          y="112"
          width="12"
          height="10"
          rx="1"
          fill="#F2ECDF"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <rect x="28" y="122" width="20" height="4" rx="1" fill="currentColor" />
        <rect x="62" y="122" width="20" height="4" rx="1" fill="currentColor" />
      </g>

      {/* Wordmark "marvisx" — condensed display font, orange "x" */}
      <text
        x="72"
        y="48"
        fontFamily="var(--pir-font-display), 'IBM Plex Sans Condensed', 'IBM Plex Sans', sans-serif"
        fontWeight="700"
        fontSize="34"
        letterSpacing="-0.5"
        fill="currentColor"
      >
        marvis<tspan fill="#F6581C">x</tspan>
      </text>

      {/* Tagline "CONSOLE · PIR" — mono, low opacity */}
      <text
        x="72"
        y="64"
        fontFamily="var(--pir-font-mono), 'JetBrains Mono', monospace"
        fontWeight="600"
        fontSize="8"
        letterSpacing="3"
        fill="currentColor"
        opacity="0.55"
      >
        CONSOLE · PIR
      </text>
    </svg>
  );
}

// Default export chooses V1/V2 from the design-v2 flag. Consumers keep using
// <Logo size="sm" /> unchanged.
export function Logo({ size = "md" }: LogoProps) {
  const v2 = useDesignV2();
  return v2 ? <LogoV2 size={size} /> : <LogoV1 size={size} />;
}
