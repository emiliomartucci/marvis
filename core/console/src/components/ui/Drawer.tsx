"use client";

import { useEffect, useRef, type ReactNode } from "react";

interface DrawerProps {
  open: boolean;
  titleId?: string;
  onClose: () => void;
  header: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  widthClassName?: string;
  dataTour?: string;
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function Drawer({
  open,
  titleId,
  onClose,
  header,
  children,
  actions,
  widthClassName = "w-[min(92vw,460px)]",
  dataTour,
}: DrawerProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const panel = panelRef.current;
    const firstFocusable = panel?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
    window.setTimeout(() => (firstFocusable ?? panel)?.focus(), 0);

    return () => {
      previousFocusRef.current?.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      if (event.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        .filter((element) => element.tabIndex >= 0);
      if (focusable.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[90] flex justify-end"
      role="presentation"
      data-testid="drawer-root"
    >
      <button
        type="button"
        aria-label="Close drawer"
        className="absolute inset-0 cursor-default bg-pir-base/70"
        onClick={onClose}
        data-testid="drawer-overlay"
      />
      <section
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-tour={dataTour}
        className={`relative flex h-full ${widthClassName} flex-col border-l border-pir bg-pir-surface-0 text-pir-text-primary shadow-xl motion-safe:transition-transform motion-safe:duration-150 motion-safe:ease-out motion-reduce:transition-none`}
      >
        <div className="shrink-0 border-b border-pir px-4 py-3">
          {header}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
          {children}
        </div>
        {actions && (
          <div className="shrink-0 border-t border-pir px-4 py-3">
            {actions}
          </div>
        )}
      </section>
    </div>
  );
}
