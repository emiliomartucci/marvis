// v2.0.0 - 2026-05-17 - Codex branch tree con tokens var(--pir-*) light-mode safe
"use client";

import { useState, type CSSProperties } from "react";

import type { BranchItem } from "./types";

export interface BranchTreeProps {
  branches: BranchItem[];
  selectedPrId: string | null;
  onSelectPr: (prId: string) => void;
  state: "active" | "stale" | "all";
  onChangeState: (s: "active" | "stale" | "all") => void;
  loading: boolean;
  error: string | null;
}

export function BranchTree({
  branches,
  selectedPrId,
  onSelectPr,
  state,
  onChangeState,
  loading,
  error,
}: BranchTreeProps) {
  return (
    <aside style={ASIDE_STYLE}>
      <header style={HEADER_STYLE}>
        <h2 style={H2_STYLE}>Branch</h2>
        <StateToggle value={state} onChange={onChangeState} />
      </header>

      <div style={LIST_WRAP_STYLE}>
        {loading && <p style={HINT_STYLE}>Carico rami…</p>}
        {error && (
          <p style={{ ...HINT_STYLE, color: "hsl(var(--pir-error))" }}>{error}</p>
        )}
        {!loading && branches.length === 0 && (
          <p style={HINT_STYLE}>Nessun ramo.</p>
        )}
        <ul style={UL_STYLE}>
          {branches.map((b) => (
            <BranchNode
              key={b.name}
              branch={b}
              selectedPrId={selectedPrId}
              onSelectPr={onSelectPr}
            />
          ))}
        </ul>
      </div>
    </aside>
  );
}

function StateToggle({
  value,
  onChange,
}: {
  value: "active" | "stale" | "all";
  onChange: (v: "active" | "stale" | "all") => void;
}) {
  const options: { value: "active" | "stale" | "all"; label: string }[] = [
    { value: "active", label: "Attivi" },
    { value: "stale", label: "Vecchi" },
    { value: "all", label: "Tutti" },
  ];
  return (
    <div style={TOGGLE_WRAP}>
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          style={value === o.value ? TOGGLE_ACTIVE : TOGGLE_IDLE}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function BranchNode({
  branch,
  selectedPrId,
  onSelectPr,
}: {
  branch: BranchItem;
  selectedPrId: string | null;
  onSelectPr: (prId: string) => void;
}) {
  const hasSelected = branch.open_pr_ids.some((id) => id === selectedPrId);
  const [expanded, setExpanded] = useState(hasSelected || branch.is_main);
  const hasPrs = branch.open_pr_ids.length > 0;

  return (
    <li style={LI_STYLE}>
      <button
        onClick={() => setExpanded((v) => !v)}
        style={BRANCH_BTN_STYLE}
        aria-expanded={expanded}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <span style={CARET_STYLE}>{expanded ? "▾" : "▸"}</span>
          <span style={BRANCH_NAME_STYLE} title={branch.name}>
            {branch.is_main && "★ "}
            {branch.name}
          </span>
        </span>
        <span style={BRANCH_META}>
          {branch.is_stale && (
            <span
              style={{
                ...META_BADGE,
                background: "hsl(var(--pir-warning) / 0.2)",
                color: "hsl(var(--pir-warning))",
              }}
            >
              vecchio
            </span>
          )}
          {hasPrs && <span style={META_TEXT}>{branch.open_pr_ids.length} PR</span>}
        </span>
      </button>
      {expanded && hasPrs && (
        <ul style={SUB_UL}>
          {branch.open_pr_ids.map((prId) => {
            const short = prId.replace(/^pr:artifact:/, "").slice(0, 8);
            const active = prId === selectedPrId;
            return (
              <li key={prId}>
                <button
                  onClick={() => onSelectPr(prId)}
                  style={active ? PR_BTN_ACTIVE : PR_BTN_IDLE}
                  title={prId}
                >
                  <span style={PR_BTN_MONO}>{short}</span>
                  <span style={PR_BTN_AGE}>
                    {branch.age_days != null ? `${branch.age_days}g` : ""}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

const ASIDE_STYLE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  width: "100%",
  minHeight: 0,
  background: "transparent",
  color: "var(--pir-text-primary)",
  fontFamily: "var(--pir-font-sans, 'IBM Plex Sans', sans-serif)",
};

const HEADER_STYLE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  borderBottom: "1px solid var(--pir-border)",
  padding: 12,
};

const H2_STYLE: CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.14em",
  textTransform: "uppercase",
  color: "var(--pir-text-secondary)",
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  margin: 0,
};

const LIST_WRAP_STYLE: CSSProperties = {
  flex: 1,
  overflow: "auto",
  padding: 8,
};

const HINT_STYLE: CSSProperties = {
  padding: "8px 4px",
  fontSize: 11,
  color: "var(--pir-text-tertiary)",
};

const UL_STYLE: CSSProperties = {
  listStyle: "none",
  padding: 0,
  margin: 0,
  display: "flex",
  flexDirection: "column",
  gap: 4,
};

const LI_STYLE: CSSProperties = {
  borderRadius: 2,
  border: "1px solid var(--pir-border)",
  background: "hsl(var(--pir-surface-1))",
};

const BRANCH_BTN_STYLE: CSSProperties = {
  display: "flex",
  width: "100%",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
  padding: "6px 8px",
  background: "transparent",
  border: "none",
  cursor: "pointer",
  fontSize: 11,
  color: "var(--pir-text-primary)",
  textAlign: "left",
  fontFamily: "inherit",
};

const CARET_STYLE: CSSProperties = {
  color: "var(--pir-text-tertiary)",
  fontSize: 10,
};

const BRANCH_NAME_STYLE: CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--pir-text-primary)",
};

const BRANCH_META: CSSProperties = {
  display: "flex",
  flexShrink: 0,
  alignItems: "center",
  gap: 6,
  fontSize: 10,
};

const META_BADGE: CSSProperties = {
  padding: "1px 6px",
  borderRadius: 2,
  fontSize: 9,
  fontWeight: 600,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
};

const META_TEXT: CSSProperties = {
  color: "var(--pir-text-tertiary)",
};

const SUB_UL: CSSProperties = {
  listStyle: "none",
  padding: "4px 8px",
  margin: 0,
  borderTop: "1px solid var(--pir-border)",
  display: "flex",
  flexDirection: "column",
  gap: 2,
};

const PR_BTN_BASE: CSSProperties = {
  display: "flex",
  width: "100%",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
  padding: "4px 8px",
  border: "none",
  borderRadius: 2,
  cursor: "pointer",
  fontSize: 11,
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  textAlign: "left",
};

const PR_BTN_IDLE: CSSProperties = {
  ...PR_BTN_BASE,
  background: "transparent",
  color: "var(--pir-text-secondary)",
};

const PR_BTN_ACTIVE: CSSProperties = {
  ...PR_BTN_BASE,
  background: "hsl(var(--pir-accent) / 0.18)",
  color: "hsl(var(--pir-accent))",
};

const PR_BTN_MONO: CSSProperties = {
  fontWeight: 500,
};

const PR_BTN_AGE: CSSProperties = {
  color: "var(--pir-text-muted)",
  fontSize: 10,
};

const TOGGLE_WRAP: CSSProperties = {
  display: "inline-flex",
  overflow: "hidden",
  border: "1px solid var(--pir-border)",
  borderRadius: 2,
  fontSize: 10,
};

const TOGGLE_BASE: CSSProperties = {
  padding: "3px 8px",
  border: "none",
  cursor: "pointer",
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
};

const TOGGLE_ACTIVE: CSSProperties = {
  ...TOGGLE_BASE,
  background: "hsl(var(--pir-accent))",
  color: "hsl(var(--pir-base))",
};

const TOGGLE_IDLE: CSSProperties = {
  ...TOGGLE_BASE,
  background: "hsl(var(--pir-surface-0))",
  color: "var(--pir-text-tertiary)",
};
