// v2.0.0 - 2026-05-17 - Codex Inspector con tokens var(--pir-*) light-mode safe
"use client";

import { type CSSProperties } from "react";
import type { ModifiedFunctionItem, TouchKind } from "./types";

const TOUCH_KIND_LABEL: Record<TouchKind, string> = {
  add: "Aggiunta",
  modify: "Modificata",
  delete: "Eliminata",
};

const TOUCH_KIND_BG: Record<TouchKind, string> = {
  add: "hsl(var(--pir-success) / 0.18)",
  modify: "hsl(var(--pir-info) / 0.18)",
  delete: "hsl(var(--pir-error) / 0.18)",
};

const TOUCH_KIND_FG: Record<TouchKind, string> = {
  add: "hsl(var(--pir-success))",
  modify: "hsl(var(--pir-info))",
  delete: "hsl(var(--pir-error))",
};

export interface PrImpactInspectorProps {
  fn: ModifiedFunctionItem | null;
  onClose: () => void;
}

export function PrImpactInspector({ fn, onClose }: PrImpactInspectorProps) {
  if (!fn) return null;
  return (
    <aside style={ASIDE_STYLE}>
      <header style={HEADER_STYLE}>
        <div style={{ minWidth: 0 }}>
          <p style={LABEL_STYLE}>Funzione</p>
          <p style={QUALIFIED_NAME_STYLE} title={fn.qualified_name_snapshot}>
            {fn.qualified_name_snapshot}
          </p>
        </div>
        <button
          onClick={onClose}
          style={CLOSE_BTN_STYLE}
          aria-label="Chiudi ispettore"
        >
          ✕
        </button>
      </header>

      <div style={BODY_STYLE}>
        <KindBadge kind={fn.touch_kind} nodeMissing={fn.node_missing} />

        <Row label="File">
          <span style={MONO_DETAIL}>{fn.source_file}</span>
        </Row>

        <Row label="Diff">
          <div style={{ display: "flex", gap: 12 }}>
            {fn.lines_added > 0 && (
              <span style={{ ...MONO_DETAIL, color: "hsl(var(--pir-success))" }}>
                +{fn.lines_added}
              </span>
            )}
            {fn.lines_removed > 0 && (
              <span style={{ ...MONO_DETAIL, color: "hsl(var(--pir-error))" }}>
                -{fn.lines_removed}
              </span>
            )}
            {fn.lines_added === 0 && fn.lines_removed === 0 && (
              <span style={{ ...MONO_DETAIL, color: "var(--pir-text-muted)" }}>—</span>
            )}
          </div>
        </Row>

        <Row label="Peso (priorità)">
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={BAR_OUTER}>
              <div
                style={{
                  ...BAR_INNER,
                  width: `${Math.round(fn.weight * 100)}%`,
                }}
              />
            </div>
            <span style={{ ...MONO_DETAIL, color: "var(--pir-text-secondary)" }}>
              {fn.weight.toFixed(2)}
            </span>
          </div>
        </Row>

        <Row label="Autore (git blame)">
          <span style={DETAIL_STYLE}>{fn.blame_author ?? "—"}</span>
        </Row>

        <Row label="Node id KG">
          <span style={{ ...MONO_TINY, wordBreak: "break-all" }} title={fn.node_id}>
            {fn.node_id}
          </span>
        </Row>

        {fn.node_missing && (
          <div style={WARN_STYLE}>
            <p style={WARN_TITLE}>Nodo orfano</p>
            <p style={WARN_BODY}>
              La funzione era nel KG ma è stata eliminata o rinominata. Manteniamo
              lo snapshot del nome per audit.
            </p>
          </div>
        )}

        <div style={PLACEHOLDER_STYLE}>
          <p style={{ ...DETAIL_STYLE, color: "var(--pir-text-secondary)" }}>
            Anteprima codice
          </p>
          <p style={{ ...DETAIL_STYLE, marginTop: 6, color: "var(--pir-text-muted)" }}>
            Disponibile in v1.1 quando il populator estrarrà signature + LOC dal
            tree-sitter (campo opzionale già previsto nello schema).
          </p>
        </div>
      </div>
    </aside>
  );
}

function KindBadge({
  kind,
  nodeMissing,
}: {
  kind: TouchKind;
  nodeMissing: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span
        style={{
          ...BADGE_BASE,
          background: TOUCH_KIND_BG[kind],
          color: TOUCH_KIND_FG[kind],
        }}
      >
        {TOUCH_KIND_LABEL[kind]}
      </span>
      {nodeMissing && (
        <span
          style={{
            ...BADGE_BASE,
            background: "hsl(var(--pir-surface-2))",
            color: "var(--pir-text-tertiary)",
          }}
        >
          orfana
        </span>
      )}
    </div>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <p style={LABEL_STYLE}>{label}</p>
      <div style={{ marginTop: 4 }}>{children}</div>
    </div>
  );
}

const ASIDE_STYLE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  width: 380,
  flexShrink: 0,
  borderLeft: "1px solid var(--pir-border)",
  background: "hsl(var(--pir-surface-0))",
  color: "var(--pir-text-primary)",
  fontFamily: "var(--pir-font-sans, 'IBM Plex Sans', sans-serif)",
};

const HEADER_STYLE: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "space-between",
  gap: 8,
  borderBottom: "1px solid var(--pir-border)",
  padding: "12px 16px",
};

const BODY_STYLE: CSSProperties = {
  flex: 1,
  overflow: "auto",
  padding: 16,
  display: "flex",
  flexDirection: "column",
  gap: 16,
  fontSize: 13,
};

const LABEL_STYLE: CSSProperties = {
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
  color: "var(--pir-text-tertiary)",
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
};

const QUALIFIED_NAME_STYLE: CSSProperties = {
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  fontSize: 13,
  color: "var(--pir-text-primary)",
  wordBreak: "break-all",
  marginTop: 2,
};

const CLOSE_BTN_STYLE: CSSProperties = {
  background: "transparent",
  border: "none",
  cursor: "pointer",
  padding: 4,
  color: "var(--pir-text-tertiary)",
  fontSize: 14,
};

const MONO_DETAIL: CSSProperties = {
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
  fontSize: 11,
  color: "var(--pir-text-secondary)",
};

const MONO_TINY: CSSProperties = {
  ...MONO_DETAIL,
  fontSize: 10,
  color: "var(--pir-text-tertiary)",
};

const DETAIL_STYLE: CSSProperties = {
  fontSize: 12,
  color: "var(--pir-text-primary)",
};

const BAR_OUTER: CSSProperties = {
  width: 128,
  height: 6,
  borderRadius: 3,
  background: "hsl(var(--pir-surface-2))",
  overflow: "hidden",
};

const BAR_INNER: CSSProperties = {
  height: "100%",
  background: "hsl(var(--pir-accent))",
};

const BADGE_BASE: CSSProperties = {
  padding: "2px 8px",
  borderRadius: 2,
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  fontFamily: "var(--pir-font-mono, 'JetBrains Mono', monospace)",
};

const WARN_STYLE: CSSProperties = {
  border: "1px solid hsl(var(--pir-warning) / 0.4)",
  background: "hsl(var(--pir-warning) / 0.1)",
  borderRadius: 3,
  padding: 12,
  fontSize: 12,
  color: "hsl(var(--pir-warning))",
};

const WARN_TITLE: CSSProperties = {
  fontWeight: 600,
};

const WARN_BODY: CSSProperties = {
  marginTop: 6,
  color: "var(--pir-text-secondary)",
};

const PLACEHOLDER_STYLE: CSSProperties = {
  border: "1px dashed var(--pir-border-strong)",
  borderRadius: 3,
  padding: 12,
};
