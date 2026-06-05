// MarvisX Console — Brain v1 WebSocket subscriber
// Pre-implementation bozza. Da copiare in console/src/lib/brain/ws.ts.
//
// Subscribe a marvisx:brain_cycle_changed per real-time PipelineSubbar + KPI updates.
// Server emit dopo ogni cycle phase completion (sub-05 §4.16).

import type { BrainCycleChangedEvent } from "./types";

type Listener = (event: BrainCycleChangedEvent) => void;

export interface BrainWsClient {
  subscribe: (listener: Listener) => () => void;
  close: () => void;
  isConnected: () => boolean;
}

export interface BrainWsConfig {
  url?: string; // default: same-origin /ws/brain
  reconnectDelayMs?: number;
  maxReconnectDelayMs?: number;
  onError?: (error: Event) => void;
  onReconnect?: () => void;
}

/**
 * Create a WebSocket client for marvisx:brain_cycle_changed events.
 *
 * Features:
 * - Auto-reconnect with exponential backoff.
 * - Pub/sub: multiple listeners can subscribe.
 * - Type-safe payloads (BrainCycleChangedEvent).
 *
 * Usage:
 * ```ts
 * const ws = createBrainWsClient();
 * const unsubscribe = ws.subscribe((event) => {
 *   if (event.phase === "done") {
 *     // refresh counters
 *   }
 * });
 * // later:
 * unsubscribe();
 * ws.close();
 * ```
 */
export function createBrainWsClient(
  config: BrainWsConfig = {}
): BrainWsClient {
  const {
    url = defaultWsUrl(),
    reconnectDelayMs = 1000,
    maxReconnectDelayMs = 30_000,
    onError,
    onReconnect,
  } = config;

  const listeners = new Set<Listener>();
  let socket: WebSocket | null = null;
  let reconnectAttempt = 0;
  let closed = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (closed) return;

    socket = new WebSocket(url);

    socket.addEventListener("open", () => {
      reconnectAttempt = 0;
      if (reconnectAttempt > 0) onReconnect?.();
    });

    socket.addEventListener("message", (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data?.type === "marvisx:brain_cycle_changed") {
          const event = data as BrainCycleChangedEvent;
          for (const listener of listeners) {
            try {
              listener(event);
            } catch (err) {
              console.error("brain ws listener error:", err);
            }
          }
        }
      } catch (err) {
        console.error("brain ws parse error:", err);
      }
    });

    socket.addEventListener("error", (e) => {
      onError?.(e);
    });

    socket.addEventListener("close", () => {
      socket = null;
      if (closed) return;
      // exponential backoff
      const delay = Math.min(
        reconnectDelayMs * Math.pow(2, reconnectAttempt),
        maxReconnectDelayMs
      );
      reconnectAttempt++;
      reconnectTimer = setTimeout(connect, delay);
    });
  }

  connect();

  return {
    subscribe(listener: Listener): () => void {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    close(): void {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (socket) socket.close();
      listeners.clear();
    },
    isConnected(): boolean {
      return socket?.readyState === WebSocket.OPEN;
    },
  };
}

function defaultWsUrl(): string {
  if (typeof window === "undefined") {
    throw new Error("createBrainWsClient must be called in browser context");
  }
  // Same WS_URL host the rest of the console uses — points at the API
  // origin (wss://api.justaskmarvis.com) so the WS terminates on the
  // FastAPI process directly, not on the Next.js console container.
  const wsBase =
    typeof process !== "undefined" && process.env.NEXT_PUBLIC_WS_URL
      ? process.env.NEXT_PUBLIC_WS_URL
      : "wss://api.justaskmarvis.com";
  return `${wsBase}/ws/brain`;
}

// ============================================================================
// React hook wrapper (optional — convenient for components)
// ============================================================================

// Note: this requires React; uncomment when copied into console/ which has React.
//
// import { useEffect, useState } from "react";
//
// export function useBrainCycleChanged(
//   onEvent: (event: BrainCycleChangedEvent) => void,
//   config?: BrainWsConfig
// ): { connected: boolean } {
//   const [connected, setConnected] = useState(false);
//
//   useEffect(() => {
//     const client = createBrainWsClient({
//       ...config,
//       onReconnect: () => setConnected(true),
//     });
//
//     const interval = setInterval(() => {
//       setConnected(client.isConnected());
//     }, 1000);
//
//     const unsubscribe = client.subscribe(onEvent);
//
//     return () => {
//       clearInterval(interval);
//       unsubscribe();
//       client.close();
//     };
//   }, []);
//
//   return { connected };
// }
