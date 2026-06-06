"use client";

import { useEffect, useState } from "react";

export type TerminalRestorePhase =
  | "opening"
  | "preflight"
  | "websocket"
  | "pty"
  | "retrying"
  | "error";

export interface TerminalRestoreState {
  sessionName: string;
  startedAt: number;
  phase: TerminalRestorePhase;
  status?: string | null;
  attempt?: number | null;
  transport?: "direct" | "tunnel" | null;
  directProbeMs?: number | null;
  ticketMs?: number | null;
  preflightMs?: number | null;
  socketOpenMs?: number | null;
  error?: string | null;
}

interface TerminalRestoreOverlayProps {
  restore: TerminalRestoreState;
  className?: string;
}

type StepState = "done" | "active" | "pending" | "error";

const PHASE_ORDER: TerminalRestorePhase[] = [
  "opening",
  "preflight",
  "websocket",
  "pty",
];

function compactMs(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return null;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.max(1, Math.round(value))}ms`;
}

function getPhaseIndex(phase: TerminalRestorePhase) {
  const index = PHASE_ORDER.indexOf(phase);
  return index === -1 ? PHASE_ORDER.length - 1 : index;
}

function getPhaseMessage(restore: TerminalRestoreState) {
  switch (restore.phase) {
    case "opening":
      return "opening session";
    case "preflight":
      return "requesting ticket and checking route";
    case "websocket":
      return "connecting websocket";
    case "pty":
      return "attaching PTY and waiting for first output";
    case "retrying":
      return "connection dropped, retrying";
    case "error":
      return "connection failed";
    default:
      return "opening terminal";
  }
}

function getStepDotClassName(state: StepState) {
  if (state === "done") return "border-pir-accent bg-pir-accent";
  if (state === "active") return "border-pir-accent bg-transparent";
  if (state === "error") return "border-pir-error bg-pir-error";
  return "border-pir-border-strong bg-transparent";
}

function getStepState({
  index,
  activeIndex,
  isError,
}: {
  index: number;
  activeIndex: number;
  isError: boolean;
}): StepState {
  if (isError) return index <= activeIndex ? "error" : "pending";
  if (index < activeIndex) return "done";
  if (index === activeIndex) return "active";
  return "pending";
}

function StepDot({ state }: { state: StepState }) {
  const className = getStepDotClassName(state);

  return (
    <span className={`flex h-2.5 w-2.5 shrink-0 rounded-full border ${className}`}>
      {state === "active" ? (
        <span className="m-auto h-1 w-1 rounded-full bg-pir-accent" />
      ) : null}
    </span>
  );
}

export function TerminalRestoreOverlay({ restore, className = "" }: TerminalRestoreOverlayProps) {
  const [now, setNow] = useState(() => performance.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(performance.now()), 250);
    return () => window.clearInterval(id);
  }, []);

  const elapsedMs = Math.max(0, now - restore.startedAt);
  const activeIndex = getPhaseIndex(restore.phase);
  const isError = restore.phase === "error";
  const stats = [
    restore.ticketMs != null ? `ticket ${compactMs(restore.ticketMs)}` : null,
    restore.directProbeMs != null ? `net ${compactMs(restore.directProbeMs)}` : null,
    restore.preflightMs != null ? `preflight ${compactMs(restore.preflightMs)}` : null,
    restore.socketOpenMs != null ? `ws ${compactMs(restore.socketOpenMs)}` : null,
    restore.transport ? restore.transport : null,
    restore.attempt && restore.attempt > 0 ? `retry ${restore.attempt}` : null,
  ].filter(Boolean);

  const steps = [
    { key: "opening", label: "opening session" },
    { key: "preflight", label: "requesting ticket" },
    { key: "websocket", label: "connecting websocket" },
    { key: "pty", label: "attaching PTY" },
  ] as const;

  return (
    <section
      aria-label={`Terminal restore status for ${restore.sessionName}`}
      aria-live="polite"
      className={[
        "pointer-events-none select-none rounded border border-pir bg-pir-surface-0/95 px-3 py-2.5 font-sans text-pir-text-secondary shadow-none",
        className,
      ].join(" ")}
      data-terminal-restore-overlay="true"
    >
      <div className="flex min-w-0 items-start gap-3">
        <div
          aria-hidden="true"
          className={[
            "mt-0.5 h-4 w-4 shrink-0 rounded-full border-2 border-pir-border-strong border-t-pir-accent",
            isError ? "border-t-pir-error" : "animate-spin",
          ].join(" ")}
        />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-baseline justify-between gap-3">
            <div className="flex min-w-0 items-baseline gap-2">
              <span className="shrink-0 text-[10px] font-semibold uppercase leading-none tracking-[0.14em] text-pir-accent">
                RESTORE
              </span>
              <span className="min-w-0 truncate font-mono text-[11px] leading-none text-pir-text-primary">
                {restore.sessionName}
              </span>
            </div>
            <span className="shrink-0 font-mono text-[10px] tabular-nums leading-none text-pir-text-muted">
              {compactMs(elapsedMs)}
            </span>
          </div>

          <p className="mt-1.5 truncate font-mono text-[11px] leading-tight text-pir-text-primary">
            {getPhaseMessage(restore)}
          </p>

          <ol className="mt-2 grid gap-1.5 sm:grid-cols-4">
            {steps.map((step, index) => {
              const state = getStepState({ index, activeIndex, isError });
              return (
                <li
                  key={step.key}
                  className="flex min-w-0 items-center gap-1.5 font-mono text-[10px] leading-none text-pir-text-muted"
                >
                  <StepDot state={state} />
                  <span className="truncate">{step.label}</span>
                </li>
              );
            })}
          </ol>

          {stats.length > 0 || restore.error ? (
            <div className="mt-2 min-w-0 truncate font-mono text-[10px] leading-none text-pir-text-muted">
              {[...stats, restore.error ? `error ${restore.error}` : null].filter(Boolean).join(" · ")}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

export default TerminalRestoreOverlay;
