export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "https://api.justaskmarvis.com";
export const WS_BASE_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? "wss://api.justaskmarvis.com";
export const DIRECT_WS_URL =
  process.env.NEXT_PUBLIC_DIRECT_WS_URL ?? "wss://term.justaskmarvis.com:8443";
export const DIRECT_WS_PROBE_URL =
  process.env.NEXT_PUBLIC_DIRECT_WS_PROBE_URL ??
  DIRECT_WS_URL.replace("wss://", "https://").replace("ws://", "http://") + "/health";
