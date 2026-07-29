"use client";

// Brain v1 — shared cycle / counters / WS context.
// Single store so every sub-route gets the same cycle envelope + live deltas.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { createBrainWsClient } from "@/lib/brain/ws";
import {
  fetchCounters,
  fetchRuns,
  recomputeCycle,
  type PipelineCounters,
} from "@/lib/brain/surfaces";
import type { BrainCycleChangedEvent, BrainRun } from "@/lib/brain/types";

type BrainScope = "company" | "program" | "project";

/** @public */
export interface BrainContextValue {
  cycleKey: string | null;
  run: BrainRun | null;
  counters: PipelineCounters | null;
  scope: BrainScope;
  setScope: (s: BrainScope) => void;
  refreshing: boolean;
  refresh: () => Promise<void>;
  loadCycle: (cycleKey: string) => Promise<void>;
  recompute: () => Promise<void>;
  recomputing: boolean;
  lastWsEvent: BrainCycleChangedEvent | null;
}

const BrainCtx = createContext<BrainContextValue | null>(null);

interface BrainProviderProps {
  userId: string;
  canRecompute: boolean;
  children: React.ReactNode;
}

export function BrainProvider({ userId, canRecompute, children }: BrainProviderProps) {
  const [cycleKey, setCycleKey] = useState<string | null>(null);
  const [run, setRun] = useState<BrainRun | null>(null);
  const [counters, setCounters] = useState<PipelineCounters | null>(null);
  const [scope, setScope] = useState<BrainScope>("company");
  const [refreshing, setRefreshing] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [lastWsEvent, setLastWsEvent] = useState<BrainCycleChangedEvent | null>(null);
  const wsRef = useRef<ReturnType<typeof createBrainWsClient> | null>(null);

  const loadCycleInternal = useCallback(async (targetCycle: string) => {
    setRefreshing(true);
    try {
      const [runsResp, countersResp] = await Promise.all([
        fetchRuns({ cycle_key: targetCycle, limit: 1 }),
        fetchCounters(targetCycle),
      ]);
      const headRun = runsResp.items?.[0] ?? null;
      setRun(headRun);
      setCycleKey(headRun?.cycle_key ?? countersResp.cycle_key ?? targetCycle);
      setCounters(countersResp);
    } finally {
      setRefreshing(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    await loadCycleInternal("latest");
  }, [loadCycleInternal]);

  const loadCycle = useCallback(
    async (cycle: string) => {
      await loadCycleInternal(cycle);
    },
    [loadCycleInternal],
  );

  const recompute = useCallback(async () => {
    if (!cycleKey || !canRecompute) return;
    setRecomputing(true);
    try {
      await recomputeCycle(cycleKey, { user_id: userId });
      await refresh();
    } finally {
      setRecomputing(false);
    }
  }, [cycleKey, canRecompute, userId, refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const client = createBrainWsClient();
    const unsubscribe = client.subscribe((event) => {
      setLastWsEvent(event);
      if (event.phase === "done") {
        void refresh();
      } else {
        setCounters((prev) =>
          prev
            ? {
                ...prev,
                digest: event.deltas?.events ?? prev.digest,
                drift: event.deltas?.drift ?? prev.drift,
                memory_ops: event.deltas?.memory_ops ?? prev.memory_ops,
                findings: event.deltas?.findings ?? prev.findings,
              }
            : prev,
        );
      }
    });
    wsRef.current = client;
    return () => {
      unsubscribe();
      client.close();
      wsRef.current = null;
    };
  }, [refresh]);

  const value = useMemo<BrainContextValue>(
    () => ({
      cycleKey,
      run,
      counters,
      scope,
      setScope,
      refreshing,
      refresh,
      loadCycle,
      recompute,
      recomputing,
      lastWsEvent,
    }),
    [cycleKey, run, counters, scope, refreshing, refresh, loadCycle, recompute, recomputing, lastWsEvent],
  );

  return <BrainCtx.Provider value={value}>{children}</BrainCtx.Provider>;
}

export function useBrainContext(): BrainContextValue {
  const ctx = useContext(BrainCtx);
  if (!ctx) {
    throw new Error("useBrainContext must be used inside BrainProvider");
  }
  return ctx;
}
