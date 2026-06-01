import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import TerminalRestoreOverlay, { type TerminalRestoreState } from "../TerminalRestoreOverlay";

const baseRestore: TerminalRestoreState = {
  sessionName: "BuildAgent",
  startedAt: performance.now() - 1250,
  phase: "preflight",
  status: "connecting",
  attempt: 0,
  ticketMs: 42,
  directProbeMs: 18,
};

describe("TerminalRestoreOverlay", () => {
  it("renders clear restore phase copy without terminal output", () => {
    render(<TerminalRestoreOverlay restore={baseRestore} />);

    expect(screen.getByLabelText("Terminal restore status for BuildAgent")).toBeInTheDocument();
    expect(screen.getByText("RESTORE")).toBeInTheDocument();
    expect(screen.getByText("BuildAgent")).toBeInTheDocument();
    expect(screen.getByText("requesting ticket and checking route")).toBeInTheDocument();
    expect(screen.getByText("opening session")).toBeInTheDocument();
    expect(screen.getByText("requesting ticket")).toBeInTheDocument();
    expect(screen.getByText("connecting websocket")).toBeInTheDocument();
    expect(screen.getByText("attaching PTY")).toBeInTheDocument();
    expect(screen.queryByText("cached output")).not.toBeInTheDocument();
  });

  it("shows websocket and transport timings when available", () => {
    render(
      <TerminalRestoreOverlay
        restore={{
          ...baseRestore,
          phase: "pty",
          transport: "direct",
          preflightMs: 310,
          socketOpenMs: 75,
        }}
      />,
    );

    expect(screen.getByText("attaching PTY and waiting for first output")).toBeInTheDocument();
    expect(screen.getByText(/ticket 42ms/)).toBeInTheDocument();
    expect(screen.getByText(/net 18ms/)).toBeInTheDocument();
    expect(screen.getByText(/preflight 310ms/)).toBeInTheDocument();
    expect(screen.getByText(/ws 75ms/)).toBeInTheDocument();
    expect(screen.getByText(/direct/)).toBeInTheDocument();
  });

  it("marks connection failures explicitly", () => {
    render(
      <TerminalRestoreOverlay
        restore={{
          ...baseRestore,
          phase: "error",
          status: "error",
          error: "Failed to fetch",
        }}
      />,
    );

    expect(screen.getByText("connection failed")).toBeInTheDocument();
    expect(screen.getByText(/error Failed to fetch/)).toBeInTheDocument();
  });
});
