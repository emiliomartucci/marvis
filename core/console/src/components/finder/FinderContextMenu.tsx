// v1.0.0 - 2026-04-22 - Right-click context menu shared between tree + list (Finder v2)
"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface ContextMenuItem {
  label?: string;
  onClick?: () => void;
  danger?: boolean;
  disabled?: boolean;
  separator?: boolean;
}

interface FinderContextMenuProps {
  x: number;
  y: number;
  items: ContextMenuItem[];
  onClose: () => void;
}

/**
 * Dismiss-on-outside-click context menu. Positioned at (x, y) viewport
 * coords, flipped into visible area if it would overflow. Rendered via
 * portal so it escapes any overflow-hidden parent.
 */
export default function FinderContextMenu({ x, y, items, onClose }: FinderContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [mounted, setMounted] = useState(false);
  const [adjusted, setAdjusted] = useState({ x, y });

  useEffect(() => {
    setMounted(true);
  }, []);

  // Flip into viewport if overflow
  useEffect(() => {
    if (!menuRef.current) return;
    const rect = menuRef.current.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let nx = x;
    let ny = y;
    if (x + rect.width > vw) nx = Math.max(4, vw - rect.width - 4);
    if (y + rect.height > vh) ny = Math.max(4, vh - rect.height - 4);
    if (nx !== adjusted.x || ny !== adjusted.y) setAdjusted({ x: nx, y: ny });
  }, [x, y, adjusted.x, adjusted.y]);

  // Dismiss: click outside, Esc, scroll
  useEffect(() => {
    const onClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onScroll = () => onClose();

    // Use mousedown to catch before click handlers inside tree
    document.addEventListener("mousedown", onClickOutside);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onClickOutside);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [onClose]);

  if (!mounted) return null;

  const menu = (
    <div
      ref={menuRef}
      role="menu"
      className="fixed z-[60] bg-pir-surface-0 border border-pir rounded-sm shadow-lg py-1 min-w-[180px]"
      style={{
        left: adjusted.x,
        top: adjusted.y,
        fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
        fontSize: 11,
      }}
    >
      {items.map((it, i) => {
        if (it.separator) {
          return <div key={`sep-${i}`} className="my-1 border-t border-pir" aria-hidden />;
        }
        let cls: string;
        if (it.disabled) cls = "text-pir-text-muted opacity-40 cursor-not-allowed";
        else if (it.danger) cls = "text-rose-400 hover:bg-rose-400/10";
        else cls = "text-pir-text-secondary hover:bg-pir-accent/10 hover:text-pir-text-primary";
        return (
          <button
            key={`${it.label}-${i}`}
            type="button"
            role="menuitem"
            disabled={it.disabled}
            onClick={() => {
              if (it.disabled) return;
              it.onClick?.();
              onClose();
            }}
            className={`w-full text-left px-3 py-1 transition-colors flex items-center gap-2 ${cls}`}
          >
            <span className="flex-1 truncate">{it.label}</span>
          </button>
        );
      })}
    </div>
  );

  return createPortal(menu, document.body);
}
