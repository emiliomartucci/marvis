"use client";

import { useEffect, useState } from "react";
import { getMonitoringCurrent } from "@/lib/api";
import type { MonitoringSnapshot } from "@/lib/types";

export function useMonitoringData() {
  const [snapshot, setSnapshot] = useState<MonitoringSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let intervalId: ReturnType<typeof setInterval> | null = null;

    const refresh = async () => {
      try {
        const data = await getMonitoringCurrent({
          signal: controller.signal,
        });
        setSnapshot(data);
        setError(null);
        setStale(false);
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError("Failed to fetch monitoring data");
        setStale(true);
      } finally {
        setLoading(false);
      }
    };

    const startPolling = () => {
      refresh();
      intervalId = setInterval(refresh, 10_000);
    };

    const stopPolling = () => {
      if (intervalId) {
        clearInterval(intervalId);
        intervalId = null;
      }
    };

    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        startPolling();
      } else {
        stopPolling();
      }
    };

    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      controller.abort();
      stopPolling();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, []);

  return { snapshot, loading, error, stale };
}
