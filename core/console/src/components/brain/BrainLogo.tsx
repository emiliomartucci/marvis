"use client";

// R1 Terminal Bot mark — Brain v1 design canonical (see
// `/data/projects/marvisx/input/design-handoff-brain-v1/reference-bot-mark-dark.svg`).
// Single-color SVG that follows the active text token so it picks up the
// theme automatically (no hex hardcoded).

/** @public */
export interface BrainLogoProps {
  size?: number;
  className?: string;
}

export function BrainLogo({ size = 28, className = "" }: BrainLogoProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-label="MarvisX Brain"
      role="img"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinejoin="miter"
      strokeLinecap="square"
    >
      <rect x="6" y="9" width="20" height="16" />
      <line x1="6" y1="14" x2="26" y2="14" />
      <rect x="9" y="17.5" width="3" height="3" fill="currentColor" stroke="none" />
      <rect x="14" y="17.5" width="3" height="3" fill="currentColor" stroke="none" />
      <rect x="19" y="17.5" width="3" height="3" fill="currentColor" stroke="none" />
      <line x1="16" y1="9" x2="16" y2="5" />
      <circle cx="16" cy="4" r="1.5" fill="currentColor" stroke="none" />
    </svg>
  );
}
