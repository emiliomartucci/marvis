// v2.0.0 - 2026-05-17 - Codex topbar con tokens var(--pir-*) light-mode safe
"use client";

import { type CSSProperties } from "react";
import type { TouchKind } from "./types";

const KIND_LABEL: Record<TouchKind, string> = {
  add: "Aggiunte",
  modify: "Modifiche",
  delete: "Eliminazioni",
};

const KIND_DOT: Record<TouchKind, string> = {
  add: "hsl(var(--pir-success))",
  modify: "hsl(var(--pir-info))",
  delete: "hsl(var(--pir-error))",
};

const POPULATOR_STYLE: Record<string, CSSProperties> = {
  processed: { background: "hsl(var(--pir-success) / 0.18)", color: "hsl(var(--pir-success))" },
  pending:   { background: "hsl(var(--pir-warning) / 0.18)", color: "hsl(var(--pir-warning))" },
  failed:    { background: "hsl(var(--pir-error) / 0.18)",   color: "hsl(var(--pir-error))" },
  unknown:   { background: "hsl(var(--pir-surface-2))",       color: "var(--pir-text-tertiary)" },
};

export interface PrImpactTopbarProps {
  populatorStatus: string;
  totalFunctions: number;
  visibleFunctions: number;
  capped: boolean;
  capThreshold: number;
  filterKinds: Set<TouchKind>;
  depth: number;
  includeAll: boolean;
  onToggleKind: (kind: TouchKind) => void;
  onDepthChange: (d: number) => void;
  onToggleIncludeAll: () => void;
}

export function PrImpactTopbar({
  populatorStatus,
  totalFunctions,
  visibleFunctions,
  capped,
  capThreshold,
  filterKinds,
  depth,
  includeAll,
  onToggleKind,
  onDepthChange,
  onToggleIncludeAll,
}: PrImpactTopbarProps) {
  return (
    <div style={WRAPPER_STYLE}>
      <PopulatorBadge status={populatorStatus} />
      <CountChip
        label="Funzioni"
        value={`${visibleFunctions} / ${totalFunctions}`}
      />
      <KindFilters filterKinds={filterKinds} onToggle={onToggleKind} />
      <DepthSlider depth={depth} onChange={onDepthChange} />
      {capped && (
        <CapToggle
          includeAll={includeAll}
          capThreshold={capThreshold}
          totalFunctions={totalFunctions}
          onToggle={onToggleIncludeAll}
        />
      )}
    </div>
  );
}

function PopulatorBadge({ status }: { status: string }) {
  const tone = POPULATOR_STYLE[status] ?? POPULATOR_STYLE.unknown;
  return (
    <span style={{ ...BADGE_BASE, ...tone }} title="Stato del populator KG">
      populator: {status}
    </span>
  );
}

function CountChip({ label, value }: { label: string; value: string }) {
  return (
    <span style={CHIP_STYLE}>
      <span style={CHIP_LABEL}>{label}</span>
      <span style={CHIP_VALUE}>{value}</span>
    </span>
  );
}

function KindFilters({
  filterKinds,
  onToggle,
}: {
  filterKinds: Set<TouchKind>;
  onToggle: (k: TouchKind) => void;
}) {
  const kinds: TouchKind[] = ["add", "modify", "delete"];
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span style={CHIP_LABEL}>Filtri</span>
      {kinds.map((k) => {
        const active = filterKinds.size === 0 || filterKinds.has(k);
        return (
          <button
            key={k}
            onClick={() => onToggle(k)}
            style={{
              ...FILTER_BTN,
              opacity: active ? 1 : 0.5,
              borderColor: active
                ? "var(--pir-border-strong)"
                : "var(--pir-border)",
              background: active
                ? "hsl(var(--pir-surface-1))"
                : "hsl(var(--pir-surface-0))",
              color: active
                ? "var(--pir-text-primary)"
                : "var(--pir-text-tertiary)",
            }}
            aria-pressed={active}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: KIND_DOT[k],
              }}
            />
            {KIND_LABEL[k]}
          </button>
        );
      })}
    </div>
  );
}

function DepthSlider({
  depth,
  onChange,
}: {
  depth: number;
  onChange: (d: number) => void;
}) {
  return (
    <label style={SLIDER_LABEL}>
      <span style={CHIP_LABEL}>Profondità</span>
      <input
        type="range"
        min={0}
        max={1}
        step={1}
        value={depth}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ accentColor: "hsl(var(--pir-accent))" }}
        aria-label="Profondità impatto transitivo"
      />
      <span style={CHIP_VALUE}>{depth}</span>
    </label>
  );
}

function CapToggle({
  includeAll,
  capThreshold,
  totalFunctions,
  onToggle,
}: {
  includeAll: boolean;
  capThreshold: number;
  totalFunctions: number;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      style={{
        ...FILTER_BTN,
        borderColor: includeAll
          ? "hsl(var(--pir-accent) / 0.6)"
          : "hsl(var(--pir-warning) / 0.6)",
        background: includeAll
          ? "hsl(var(--pir-accent) / 0.12)"
          : "hsl(var(--pir-warning) / 0.12)",
        color: includeAll ? "hsl(var(--pir-accent))" : "hsl(var(--pir-warning))",
      }}
      title={`La PR tocca ${totalFunctions} funzioni, ne mostriamo ${capThreshold} per default`}
    >
      {includeAll
        ? `Mostro tutte ${totalFunctions}`
        : `Mostra tutte ${totalFunctions} (lento)`}
    </button>
  );
}

const WRAPPER_STYLE: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  gap: 14,
  padding: "8px 16px",
  borderBottom: "1px solid var(--pir-border)",
  background: "hsl(var(--pir-surface-0))",
  color: "var(--pir-text-secondary)",
  fontFamily: "var(--pir-font-sans, 'IBM Plex Sans', sans-serif)",
};

const BADGE_BASE: CSSProperties = {
  padding: "3px 8px",
  borderRadius: 2,
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
};

const CHIP_STYLE: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  fontSize: 11,
};

const CHIP_LABEL: CSSProperties = {
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.14em",
  textTransform: "uppercase",
  color: "var(--pir-text-tertiary)",
};

const CHIP_VALUE: CSSProperties = {
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  fontSize: 11,
  fontWeight: 500,
  color: "var(--pir-text-primary)",
};

const FILTER_BTN: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "4px 8px",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  fontSize: 11,
  fontFamily: "var(--pir-font-sans, 'IBM Plex Sans', sans-serif)",
  cursor: "pointer",
};

const SLIDER_LABEL: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  fontSize: 11,
  color: "var(--pir-text-tertiary)",
};
