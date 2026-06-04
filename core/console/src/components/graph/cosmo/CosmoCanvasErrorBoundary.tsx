// v1.0.0 - 2026-04-24 - Error boundary canvas Cosmo (M-FE-16 piano).
//
// Wrappa SOLO il GraphCanvas. Il GraphInspector sopravvive a un crash del
// canvas: meglio mostrare l'inspector sul nodo selezionato che un panel
// bianco con pulsante reload.
"use client";

import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export class CosmoCanvasErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: unknown, info: unknown): void {
    // Il log arriva in Sentry/browser devtools; nessun re-throw.
    console.error("[graph] canvas boundary:", error, info);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 12,
            background: "hsl(var(--pir-base))",
            color: "var(--pir-text-secondary)",
            fontFamily: "var(--pir-font-mono)",
            fontSize: 12,
            letterSpacing: "0.08em",
          }}
        >
          <div style={{ textTransform: "uppercase" }}>
            graph canvas error
          </div>
          <button
            type="button"
            onClick={() => {
              if (typeof window !== "undefined") window.location.reload();
            }}
            style={{
              padding: "6px 12px",
              background: "transparent",
              border: "1px solid var(--pir-border)",
              borderRadius: 2,
              color: "hsl(var(--pir-accent))",
              fontFamily: "inherit",
              fontSize: 11,
              letterSpacing: "0.12em",
              textTransform: "uppercase",
              cursor: "pointer",
            }}
          >
            reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
