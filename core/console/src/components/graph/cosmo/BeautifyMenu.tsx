// v1.2.0 - 2026-04-24 - useIsClient via useEffect (canonical 2-row pattern).
//                       Il noop-subscribe di useSyncExternalStore era corretto
//                       ma opaco: sostituito per ridurre superficie di bug.
// v1.1.0 - 2026-04-24 - createPortal per evitare clip dal canvas overflow:hidden
// v1.0.0 - 2026-04-24 - Menu "Beautify" (4 opzioni) per ridisposizione layout.
//
// Dropdown aperto sopra il trigger. Il toast e la tween RAF vivono in
// GraphCanvas (animRef condiviso, D-03 piano: user pan cancella). Qui solo
// UI chrome del menu.
//
// Il dropdown e' montato via createPortal a document.body con position:fixed:
// il container canvas root ha overflow:hidden (richiesto per pan/zoom clipping)
// che altrimenti clippava il dropdown quando piu' alto del trigger HUD.
"use client";

import {
  memo,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
import type { BeautifyKind } from "./types";

// Canonical mount-flag: false on server / first client render (matches SSR),
// flips to true in an effect post-hydration. Semplice, lint-neutral.
function useIsClient(): boolean {
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);
  return mounted;
}

interface BeautifyMenuProps {
  /** Menu aperto / chiuso. Controllato dal parent (HudShortcuts). */
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  onBeautify: (kind: BeautifyKind) => void;
}

interface BeautifyOpt {
  kind: BeautifyKind;
  icon: string;
  title: string;
  desc: string;
  dim?: boolean;
}

const OPTIONS: readonly BeautifyOpt[] = [
  { kind: "constellation",  icon: "◉", title: "Constellation",  desc: "orbite concentriche per depth" },
  { kind: "galaxy",         icon: "❂", title: "Galaxy arms",    desc: "spirali per program" },
  { kind: "grappolo",       icon: "✸", title: "Grappolo",       desc: "cluster denso, cerchi vicini" },
  { kind: "sistema-solare", icon: "☼", title: "Sistema solare", desc: "orbite larghe, pianeti distanti" },
  { kind: "reset",          icon: "↺", title: "Reset positions", desc: "clear all drags", dim: true },
];

/**
 * Single row option. Inline in BeautifyMenu (no shared chrome file, H-03 piano).
 */
function BeautifyRow({ opt, onClick }: { opt: BeautifyOpt; onClick: () => void }) {
  const [hover, setHover] = useState(false);
  const style: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "8px 10px",
    background: hover ? "hsl(var(--pir-accent) / 0.12)" : "transparent",
    border: `1px solid ${hover ? "hsl(var(--pir-accent) / 0.4)" : "transparent"}`,
    borderRadius: 2,
    color: opt.dim ? "var(--pir-text-muted)" : "var(--pir-text-primary)",
    fontFamily: "var(--pir-font-sans)",
    fontSize: 12,
    textAlign: "left",
    cursor: "pointer",
    transition: "background 80ms ease, border-color 80ms ease",
    width: "100%",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={style}
    >
      <span
        style={{
          width: 22,
          height: 22,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          color: hover ? "hsl(var(--pir-accent))" : "var(--pir-text-tertiary)",
          fontSize: 14,
        }}
      >
        {opt.icon}
      </span>
      <span style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        <span style={{ fontWeight: 500, lineHeight: 1.1 }}>{opt.title}</span>
        <span
          style={{
            fontFamily: "var(--pir-font-mono)",
            fontSize: 9,
            color: "var(--pir-text-muted)",
            letterSpacing: "0.04em",
            lineHeight: 1.2,
          }}
        >
          {opt.desc}
        </span>
      </span>
    </button>
  );
}

interface DropdownPosition {
  top: number;
  right: number;
}

/**
 * Menu Beautify con trigger "✦ Beautify" e 4 opzioni layout.
 * @public
 */
function BeautifyMenuImpl({ open, onToggle, onClose, onBeautify }: BeautifyMenuProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const mounted = useIsClient();
  const [pos, setPos] = useState<DropdownPosition | null>(null);

  // Measure trigger + compute dropdown anchor (top-right relativo a viewport).
  // Anchor = sopra il trigger (bottom edge del dropdown ~6px sopra trigger top),
  // allineato a destra (right edge condiviso con trigger right).
  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    if (!trigger) return;
    const recompute = () => {
      const rect = trigger.getBoundingClientRect();
      const DROPDOWN_HEIGHT_APPROX = 220; // 4 rows ~52px + padding
      const GAP = 6;
      setPos({
        top: Math.max(8, rect.top - DROPDOWN_HEIGHT_APPROX - GAP),
        right: Math.max(8, window.innerWidth - rect.right),
      });
    };
    recompute();
    window.addEventListener("resize", recompute);
    window.addEventListener("scroll", recompute, true);
    return () => {
      window.removeEventListener("resize", recompute);
      window.removeEventListener("scroll", recompute, true);
    };
  }, [open]);

  // Click-outside: chiude il menu quando si clicka fuori trigger + dropdown.
  useEffect(() => {
    if (!open) return undefined;
    const onDocDown = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (triggerRef.current?.contains(target)) return;
      const dropdown = document.getElementById("cosmo-beautify-dropdown");
      if (dropdown?.contains(target)) return;
      onClose();
    };
    document.addEventListener("mousedown", onDocDown);
    return () => document.removeEventListener("mousedown", onDocDown);
  }, [open, onClose]);

  const triggerStyle: CSSProperties = {
    padding: "4px 10px",
    background: open ? "hsl(var(--pir-accent) / 0.14)" : "hsl(var(--pir-surface-1))",
    border: `1px solid ${open ? "hsl(var(--pir-accent) / 0.5)" : "var(--pir-border)"}`,
    borderRadius: 2,
    color: open ? "hsl(var(--pir-accent))" : "var(--pir-text-primary)",
    fontFamily: "var(--pir-font-mono)",
    fontSize: 10,
    letterSpacing: "0.12em",
    textTransform: "uppercase",
    cursor: "pointer",
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
  };

  const dropdownStyle: CSSProperties = {
    position: "fixed",
    top: pos?.top ?? 0,
    right: pos?.right ?? 0,
    minWidth: 240,
    background: "hsl(var(--pir-surface-0))",
    border: "1px solid var(--pir-border-strong)",
    borderRadius: 2,
    boxShadow: "0 8px 24px -4px hsl(0 0% 0% / 0.4)",
    padding: 4,
    display: "flex",
    flexDirection: "column",
    gap: 2,
    zIndex: 40,
    visibility: pos ? "visible" : "hidden",
  };

  const dropdownNode =
    open && mounted && pos ? (
      <div id="cosmo-beautify-dropdown" role="menu" style={dropdownStyle}>
        {OPTIONS.map((opt, i) => (
          <div key={opt.kind}>
            {opt.dim && i > 0 && (
              <div style={{ height: 1, background: "var(--pir-border)", margin: "4px 0" }} />
            )}
            <BeautifyRow
              opt={opt}
              onClick={() => {
                onBeautify(opt.kind);
                onClose();
              }}
            />
          </div>
        ))}
      </div>
    ) : null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={onToggle}
        style={triggerStyle}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span>✦ Beautify</span>
        <span style={{ fontSize: 8, opacity: 0.7 }}>{open ? "▴" : "▾"}</span>
      </button>
      {mounted && dropdownNode && createPortal(dropdownNode, document.body)}
    </>
  );
}

export const BeautifyMenu = memo(BeautifyMenuImpl);
