export const APP_BASE_PATH = "/ui";
export const CONSOLE_LOGIN_PATH = `${APP_BASE_PATH}/login/`;
export const SERVICE_WORKER_PATH = `${APP_BASE_PATH}/sw.js`;
export const SERVICE_WORKER_SCOPE = `${APP_BASE_PATH}/`;
export const MANIFEST_PATH = `${APP_BASE_PATH}/manifest.webmanifest`;

export function redirectToConsoleLogin(
  navigate: (url: string) => void = (url) => window.location.assign(url)
): void {
  navigate(CONSOLE_LOGIN_PATH);
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function browserWsOrigin(): string {
  if (typeof window === "undefined") return "";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}`;
}

export const API_BASE_URL = trimTrailingSlash(
  process.env.NEXT_PUBLIC_API_URL ?? ""
);
export const WS_BASE_URL = trimTrailingSlash(process.env.NEXT_PUBLIC_WS_URL ?? "");
export const DIRECT_WS_URL = trimTrailingSlash(
  process.env.NEXT_PUBLIC_DIRECT_WS_URL ?? ""
);
export const DIRECT_WS_PROBE_URL = trimTrailingSlash(
  process.env.NEXT_PUBLIC_DIRECT_WS_PROBE_URL ?? ""
);

export function getWsBaseUrl(): string {
  return WS_BASE_URL || browserWsOrigin();
}

export function getDirectWsBaseUrl(): string {
  return DIRECT_WS_URL || getWsBaseUrl();
}

export function getDirectWsProbeUrl(): string {
  if (DIRECT_WS_PROBE_URL) return DIRECT_WS_PROBE_URL;
  const directWsUrl = getDirectWsBaseUrl();
  if (!directWsUrl) return "";
  return `${directWsUrl.replace("wss://", "https://").replace("ws://", "http://")}/health`;
}
