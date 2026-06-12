// v1.0.0 - 2026-05-17 - Beautify menu Codex (PR4 unify).
//
// Dropdown semplificato vs Cosmo BeautifyMenu: 2 preset (grappolo +
// sistema-solare) + reset. Stesso visual language di Cosmo (createPortal,
// position fixed, hover bone/accent).
"use client";

import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";

import type { CodexBeautifyKind } from "./codexLayouts";

interface CodexBeautifyOpt {
  kind: CodexBeautifyKind;
  icon: string;
  title: string;
  desc: string;
  dim?: boolean;
}

const OPTIONS: readonly CodexBeautifyOpt[] = [
  { kind: "constellation",  icon: "◉", title: "Constellation",  desc: "orbite concentriche per cluster" },
  { kind: "galaxy",         icon: "❂", title: "Galaxy arms",    desc: "spirali per cluster" },
  { kind: "grappolo",       icon: "✸", title: "Grappolo",       desc: "cluster denso, moduli vicini" },
  { kind: "sistema-solare", icon: "☼", title: "Sistema solare", desc: "orbite larghe, moduli distanti" },
  { kind: "reset",          icon: "↺", title: "Reset",          desc: "torna al layout iniziale", dim: true },
];

interface CodexBeautifyMenuProps {
  onBeautify: (kind: CodexBeautifyKind) => void;
}

function useIsClient(): boolean {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}

function Row({ opt, onClick }: { opt: CodexBeautifyOpt; onClick: () => void }) {
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
      <span style={{ width: 20, textAlign: "center", fontSize: 14 }}>{opt.icon}</span>
      <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontWeight: 600 }}>{opt.title}</span>
        <span style={{ fontSize: 10, color: "var(--pir-text-muted)" }}>{opt.desc}</span>
      </span>
    </button>
  );
}

/**
 * Beautify menu Codex. Dropdown sopra il trigger button.
 * @public
 */
export function CodexBeautifyMenu({ onBeautify }: CodexBeautifyMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const mounted = useIsClient();

  useEffect(() => {
    if (!open) return undefined;
    const onClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current && triggerRef.current.contains(target)) return;
      const menu = document.getElementById("codex-beautify-menu");
      if (menu && menu.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("click", onClick);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", onClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const trigger = (
    <button
      ref={triggerRef}
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        if (triggerRef.current) setRect(triggerRef.current.getBoundingClientRect());
        setOpen((o) => !o);
      }}
      style={{
        ...TRIGGER_STYLE,
        background: open ? "hsl(var(--pir-accent) / 0.15)" : TRIGGER_STYLE.background,
        borderColor: open ? "hsl(var(--pir-accent))" : "var(--pir-border)",
        color: open ? "hsl(var(--pir-accent))" : TRIGGER_STYLE.color,
      }}
    >
      <span style={{ marginRight: 4 }}>✸</span>
      Beautify
      <span style={{ marginLeft: 6, opacity: 0.6, fontSize: 8 }}>{open ? "▴" : "▾"}</span>
    </button>
  );

  if (!mounted || !open || !rect) return trigger;

  return (
    <>
      {trigger}
      {createPortal(
        <div
          id="codex-beautify-menu"
          style={{
            ...MENU_STYLE,
            top: rect.top - 8,
            left: rect.left,
            transform: "translateY(-100%)",
          }}
        >
          {OPTIONS.map((opt) => (
            <Row
              key={opt.kind}
              opt={opt}
              onClick={() => {
                onBeautify(opt.kind);
                setOpen(false);
              }}
            />
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}

const TRIGGER_STYLE: CSSProperties = {
  height: 26,
  padding: "0 12px",
  display: "inline-flex",
  alignItems: "center",
  background: "transparent",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  color: "var(--pir-text-secondary)",
  fontFamily: "var(--pir-font-mono)",
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  cursor: "pointer",
};

const MENU_STYLE: CSSProperties = {
  position: "fixed",
  zIndex: 100,
  minWidth: 220,
  padding: 6,
  background: "hsl(var(--pir-surface-0) / 0.98)",
  backdropFilter: "blur(8px)",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  boxShadow: "0 8px 24px hsl(0 0% 0% / 0.16)",
  display: "flex",
  flexDirection: "column",
  gap: 2,
};
