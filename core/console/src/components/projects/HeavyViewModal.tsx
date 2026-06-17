// v1.0.0 - 2026-04-22 - Modal shell + wrappers for /projects heavy views (PR #9)
"use client";

import { useEffect, type ReactNode } from "react";

interface HeavyViewModalProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
}

function HeavyViewModal({ title, onClose, children }: HeavyViewModalProps) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.60)", backdropFilter: "blur(2px)" }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="bg-pir-surface-0 border border-pir flex flex-col overflow-hidden"
        style={{
          borderRadius: 10,
          width: "min(92vw, 1200px)",
          maxHeight: "calc(100vh - 32px)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <header
          className="flex items-center justify-between border-b border-pir shrink-0"
          style={{ padding: "12px 18px" }}
        >
          <h2 className="text-pir-text-primary font-semibold text-[15px] m-0">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-pir-text-muted hover:text-pir-text-primary transition-colors bg-transparent border-0 cursor-pointer"
            style={{ fontSize: 22, lineHeight: 1, padding: "0 4px" }}
          >
            ×
          </button>
        </header>
        <div className="flex-1 overflow-auto" style={{ padding: "18px 22px" }}>
          {children}
        </div>
      </div>
    </div>
  );
}

export default HeavyViewModal;
