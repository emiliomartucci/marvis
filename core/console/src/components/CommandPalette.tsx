"use client";

import { useEffect, useRef, useState } from "react";
import type { Session } from "@/lib/types";

interface CommandPaletteProps {
  sessions: Session[];
  onSelect: (name: string) => void;
  onClose: () => void;
}

export default function CommandPalette({
  sessions,
  onSelect,
  onClose,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const filtered = sessions.filter((s) => {
    const q = query.toLowerCase();
    return (
      s.name.toLowerCase().includes(q) ||
      (s.display_name && s.display_name.toLowerCase().includes(q)) ||
      (s.project_slug && s.project_slug.toLowerCase().includes(q))
    );
  });

  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  // Scroll selected item into view
  useEffect(() => {
    if (!listRef.current) return;
    const items = listRef.current.querySelectorAll("[data-palette-item]");
    items[selectedIndex]?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && filtered.length > 0) {
      e.preventDefault();
      onSelect(filtered[selectedIndex].name);
      onClose();
    } else if (e.key === "Escape") {
      onClose();
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[18vh] bg-black/50"
      onClick={onClose}
    >
      <div
        className="bg-pir-surface-0 border border-pir rounded-lg w-full max-w-md shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Switch session..."
          className="w-full px-4 py-3 bg-transparent border-b border-pir text-pir-text-primary focus:outline-none text-sm font-mono"
          autoFocus
        />
        <div ref={listRef} className="max-h-64 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-pir-text-muted">
              No sessions match
            </div>
          ) : (
            filtered.map((session, i) => (
              <button
                key={session.name}
                data-palette-item
                onClick={() => {
                  onSelect(session.name);
                  onClose();
                }}
                className={`w-full px-4 py-2.5 flex items-center gap-3 text-left transition-colors ${
                  i === selectedIndex
                    ? "bg-pir-surface-1"
                    : "hover:bg-pir-surface-1/60"
                }`}
              >
                {/* Pin indicator */}
                {session.pinned && (
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" className="text-pir-accent shrink-0">
                    <path d="M10 1L6 5L2 6L5.5 9.5L3 14L7 10.5L10 14L11 10L15 6L10 1Z" />
                  </svg>
                )}

                <div className="flex flex-col min-w-0 flex-1">
                  <span className="font-mono text-sm text-pir-text-primary truncate">
                    {session.name}
                  </span>
                  {(session.display_name || session.project_slug) && (
                    <span className="text-[11px] text-pir-text-muted truncate">
                      {[session.project_slug, session.display_name]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  )}
                </div>

                {/* Status */}
                {session.status && session.status !== "bash" && session.status !== "zsh" && (
                  <span className="text-[10px] font-mono text-pir-text-muted shrink-0">
                    {session.status}
                  </span>
                )}

                {/* Keyboard shortcut hint for selected */}
                {i === selectedIndex && (
                  <span className="text-[10px] text-pir-text-tertiary shrink-0">
                    Enter
                  </span>
                )}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
