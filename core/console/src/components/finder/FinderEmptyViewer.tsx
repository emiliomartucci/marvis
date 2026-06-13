// v1.0.0 - 2026-04-22 - Finder v2 viewer empty state: welcome card + tiles (PR #10)
"use client";

interface FinderEmptyViewerProps {
  onOpenSearch: () => void;
}

export default function FinderEmptyViewer({ onOpenSearch }: FinderEmptyViewerProps) {
  return (
    <div className="flex flex-col items-center justify-center h-full bg-pir-base px-6">
      <div className="max-w-md w-full text-center">
        <div
          className="mx-auto mb-4 flex items-center justify-center border border-pir rounded-sm"
          style={{ width: 48, height: 48 }}
          aria-hidden
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            className="text-pir-text-tertiary"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
        </div>
        <p
          className="text-pir-text-primary mb-2"
          style={{
            fontFamily: "var(--pir-font-sans, ui-sans-serif, system-ui)",
            fontWeight: 600,
            fontSize: 14,
            lineHeight: 1.3,
          }}
        >
          Pick a file from the tree
        </p>
        <p
          className="text-pir-text-tertiary mb-5"
          style={{
            fontFamily: "var(--pir-font-sans, ui-sans-serif, system-ui)",
            fontSize: 12,
            lineHeight: 1.5,
          }}
        >
          or open the palette with{" "}
          <kbd
            className="inline-block border border-pir rounded-[2px] bg-pir-surface-1 text-pir-text-secondary"
            style={{
              fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
              fontSize: 10,
              padding: "1px 5px",
              verticalAlign: "middle",
            }}
          >
            ⌘K
          </kbd>{" "}
          to search.
        </p>

        <div className="grid grid-cols-3 gap-2">
          <Tile title="Recent" hint="Last opened" />
          <Tile title="Pinned" hint="Starred paths" />
          <Tile title="Hotspot" hint="Touched often" />
        </div>

        <button
          type="button"
          onClick={onOpenSearch}
          className="mt-5 inline-flex items-center gap-1.5 border border-pir hover:border-pir-accent/50 text-pir-text-tertiary hover:text-pir-accent transition-colors"
          style={{
            fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            padding: "5px 10px",
            borderRadius: 2,
          }}
        >
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          Open search
        </button>
      </div>
    </div>
  );
}

function Tile({ title, hint }: { title: string; hint: string }) {
  return (
    <div
      className="border border-pir rounded-sm text-left p-3 opacity-60"
      aria-hidden
    >
      <div
        className="text-pir-text-tertiary uppercase mb-1"
        style={{
          fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
          fontSize: 9,
          letterSpacing: "0.18em",
        }}
      >
        {title}
      </div>
      <div
        className="text-pir-text-muted"
        style={{
          fontFamily: "var(--pir-font-mono, ui-monospace, monospace)",
          fontSize: 10,
          fontWeight: 500,
        }}
      >
        {hint}
      </div>
      {/* Phase 1: placeholder. Phase 2 will wire to real recent/pinned/hotspot data. */}
    </div>
  );
}
