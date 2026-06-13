// v1.0.0 - 2026-03-13 - Reusable polling hook with visibility pause and adaptive backoff
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

interface UsePollingDataOptions {
  /** Polling interval in ms */
  interval: number;
  /** Disable polling (e.g. when all checks completed) */
  enabled?: boolean;
  /** Enable adaptive backoff: doubles interval after unchangedThreshold consecutive unchanged responses */
  backoff?: boolean;
  /** Number of unchanged responses before backoff kicks in (default: 3) */
  unchangedThreshold?: number;
  /** Maximum backoff interval in ms (default: 4x base interval) */
  maxInterval?: number;
}

interface UsePollingDataResult<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  /** Force an immediate refresh */
  refresh: () => void;
}

export function usePollingData<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  options: UsePollingDataOptions
): UsePollingDataResult<T> {
  const {
    interval,
    enabled = true,
    backoff = false,
    unchangedThreshold = 3,
    maxInterval = interval * 4,
  } = options;

  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const controllerRef = useRef<AbortController | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastDataRef = useRef<string>("");
  const unchangedCountRef = useRef(0);
  const currentIntervalRef = useRef(interval);

  const fetchData = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    try {
      const result = await fetcher(controller.signal);
      const serialized = JSON.stringify(result);

      if (backoff && serialized === lastDataRef.current) {
        unchangedCountRef.current++;
      } else {
        unchangedCountRef.current = 0;
        currentIntervalRef.current = interval;
      }
      lastDataRef.current = serialized;

      setData(result);
      setError(null);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e : new Error("Fetch failed"));
    } finally {
      setLoading(false);
    }
  }, [fetcher, backoff, interval]);

  const startPolling = useCallback(() => {
    fetchData();
    if (intervalRef.current) clearInterval(intervalRef.current);

    let effectiveInterval = currentIntervalRef.current;
    if (backoff && unchangedCountRef.current >= unchangedThreshold) {
      effectiveInterval = Math.min(effectiveInterval * 2, maxInterval);
      currentIntervalRef.current = effectiveInterval;
    }

    intervalRef.current = setInterval(fetchData, effectiveInterval);
  }, [fetchData, backoff, unchangedThreshold, maxInterval]);

  const stopPolling = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      stopPolling();
      return;
    }

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
      controllerRef.current?.abort();
      stopPolling();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [enabled, startPolling, stopPolling]);

  const refresh = useCallback(() => {
    unchangedCountRef.current = 0;
    currentIntervalRef.current = interval;
    fetchData();
  }, [fetchData, interval]);

  return { data, loading, error, refresh };
}
