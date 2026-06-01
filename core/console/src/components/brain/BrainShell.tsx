"use client";

import { useCallback } from "react";

import { BrainProvider, useBrainContext } from "./useBrainContext";
import { BrainSidebar } from "./BrainSidebar";
import { PipelineSubbar } from "./PipelineSubbar";

interface BrainShellProps {
  userId?: string;
  canRecompute?: boolean;
  children: React.ReactNode;
}

function BrainShellBody({ children }: { children: React.ReactNode }) {
  const ctx = useBrainContext();
  const onScopeChange = useCallback(
    (scope: "company" | "program" | "project") => ctx.setScope(scope),
    [ctx],
  );
  const onCycleChange = useCallback(
    (cycle: string) => {
      ctx.loadCycle(cycle).catch(() => {
        /* surface to console via fetch error; toast wired in future */
      });
    },
    [ctx],
  );
  const onRecompute = useCallback(() => {
    ctx.recompute().catch(() => {
      /* surface to console via fetch error; toast wired in future */
    });
  }, [ctx]);
  return (
    <div className="flex h-full min-h-[600px] flex-col bg-[hsl(var(--pir-base))] text-pir-text-primary">
      <PipelineSubbar counters={ctx.counters} loading={ctx.refreshing} />
      <div className="flex flex-1 overflow-hidden">
        <BrainSidebar
          cycleKey={ctx.cycleKey}
          scope={ctx.scope}
          onScopeChange={onScopeChange}
          onCycleChange={onCycleChange}
          canRecompute={Boolean(ctx.recompute)}
          recomputeMode={ctx.run?.trigger ?? "shadow"}
          onRecompute={onRecompute}
          recomputing={ctx.recomputing}
        />
        <main
          className="flex-1 overflow-y-auto bg-[hsl(var(--pir-surface-0))] px-6 py-5"
          style={{ borderRadius: "2px" }}
        >
          {children}
        </main>
      </div>
    </div>
  );
}

export function BrainShell({ userId = "anonymous", canRecompute = false, children }: BrainShellProps) {
  return (
    <BrainProvider userId={userId} canRecompute={canRecompute}>
      <BrainShellBody>{children}</BrainShellBody>
    </BrainProvider>
  );
}
