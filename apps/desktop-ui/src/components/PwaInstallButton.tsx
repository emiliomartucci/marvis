"use client";

import { useEffect, useState } from "react";

import { SERVICE_WORKER_PATH, SERVICE_WORKER_SCOPE } from "@/lib/config";

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
};

function getNextBuildId(): string {
  const nextData = (window as { __NEXT_DATA__?: { buildId?: string } }).__NEXT_DATA__;
  if (nextData?.buildId) return nextData.buildId;

  const asset = document.querySelector<HTMLScriptElement | HTMLLinkElement>(
    'script[src*="/_next/static/"],link[href*="/_next/static/"]'
  );
  const assetUrl = asset instanceof HTMLScriptElement ? asset.src : asset?.href;
  const hash = assetUrl?.match(/[-/]([a-f0-9]{8,})(?:\\.js|\\.css|\\.woff2?)$/)?.[1];
  return hash ?? "static";
}

function serviceWorkerUrl(): string {
  return `${SERVICE_WORKER_PATH}?v=${encodeURIComponent(getNextBuildId())}`;
}

export function PwaInstallButton() {
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null);
  const [installed, setInstalled] = useState(false);

  useEffect(() => {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker
        .register(serviceWorkerUrl(), { scope: SERVICE_WORKER_SCOPE })
        .catch((error: unknown) => {
          console.warn("Service worker registration failed:", error);
        });
    }

    function onBeforeInstallPrompt(event: Event) {
      event.preventDefault();
      setInstallPrompt(event as BeforeInstallPromptEvent);
    }

    function onInstalled() {
      setInstalled(true);
      setInstallPrompt(null);
    }

    window.addEventListener("beforeinstallprompt", onBeforeInstallPrompt);
    window.addEventListener("appinstalled", onInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", onBeforeInstallPrompt);
      window.removeEventListener("appinstalled", onInstalled);
    };
  }, []);

  async function handleInstall() {
    if (!installPrompt) return;
    await installPrompt.prompt();
    const choice = await installPrompt.userChoice;
    if (choice.outcome !== "dismissed") setInstallPrompt(null);
  }

  if (!installPrompt || installed) return null;

  return (
    <button
      type="button"
      onClick={handleInstall}
      className="px-2 text-pir-text-tertiary hover:text-pir-text-primary transition-colors font-mono"
      style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase", height: 30 }}
    >
      Install
    </button>
  );
}
