// v1.0.0 - 2026-05-17 - Lens switcher Universe / Cosmo / Codex per /graph
//
// Tre pulsanti per cambiare la lente attiva. Lo stato vive nella query string
// (`?lens=`) cosi' refresh e share-link funzionano. Match design canonical
// da /data/projects/marvisx/input/design-handoff-codex-v1/proposed-lib/codex-page.jsx
"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type CSSProperties } from "react";

const LENSES = [
  { id: "universe", label: "Universe", desc: "progetti" },
  { id: "cosmo", label: "Cosmo", desc: "satelliti per progetto" },
  { id: "codex", label: "Codex", desc: "codice · moduli · funzioni" },
] as const;

export type LensId = (typeof LENSES)[number]["id"];

export function LensSwitcher({
  active,
  className,
}: {
  active: LensId;
  className?: string;
}) {
  const params = useSearchParams();
  return (
    <div style={WRAPPER_STYLE} className={className}>
      {LENSES.map((l) => {
        const isActive = l.id === active;
        // Preserve other query params (e.g. ?pr=...) when switching lens.
        const next = new URLSearchParams(params?.toString() ?? "");
        next.set("lens", l.id);
        const href = `/graph?${next.toString()}`;
        return (
          <Link
            key={l.id}
            href={href}
            style={isActive ? BTN_ACTIVE : BTN_IDLE}
            aria-current={isActive ? "page" : undefined}
            title={l.desc}
          >
            <span style={isActive ? LABEL_ACTIVE : LABEL_IDLE}>{l.label}</span>
            <span style={isActive ? DESC_ACTIVE : DESC_IDLE}>{l.desc}</span>
          </Link>
        );
      })}
    </div>
  );
}

const WRAPPER_STYLE: CSSProperties = {
  display: "inline-flex",
  background: "hsl(var(--pir-surface-0))",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  padding: 2,
  gap: 2,
  pointerEvents: "auto",
};

const BTN_BASE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "flex-start",
  padding: "6px 12px 7px",
  border: "none",
  borderRadius: 2,
  cursor: "pointer",
  minWidth: 110,
  textDecoration: "none",
};

const BTN_ACTIVE: CSSProperties = {
  ...BTN_BASE,
  background: "hsl(var(--pir-surface-2))",
};

const BTN_IDLE: CSSProperties = {
  ...BTN_BASE,
  background: "transparent",
};

const LABEL_BASE: CSSProperties = {
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.12em",
  textTransform: "uppercase",
  lineHeight: 1,
};

const LABEL_ACTIVE: CSSProperties = {
  ...LABEL_BASE,
  color: "var(--pir-text-primary)",
};

const LABEL_IDLE: CSSProperties = {
  ...LABEL_BASE,
  color: "var(--pir-text-tertiary)",
};

const DESC_BASE: CSSProperties = {
  marginTop: 3,
  fontFamily: "var(--pir-font-sans, 'IBM Plex Sans', sans-serif)",
  fontSize: 10,
  fontWeight: 500,
  letterSpacing: "0.01em",
  lineHeight: 1,
};

const DESC_ACTIVE: CSSProperties = {
  ...DESC_BASE,
  color: "hsl(var(--pir-accent))",
};

const DESC_IDLE: CSSProperties = {
  ...DESC_BASE,
  color: "var(--pir-text-muted)",
};
