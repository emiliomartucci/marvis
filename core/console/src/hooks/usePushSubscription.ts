"use client";

import { useCallback, useEffect, useState } from "react";
import { API_BASE_URL } from "@/lib/config";

type PushState = "unsupported" | "prompt" | "denied" | "subscribed" | "loading";

const VAPID_PUBLIC_KEY = process.env.NEXT_PUBLIC_VAPID_PUBLIC_KEY ?? "";

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from(rawData, (char) => char.charCodeAt(0));
}

export function usePushSubscription() {
  const [state, setState] = useState<PushState>("loading");

  useEffect(() => {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !VAPID_PUBLIC_KEY) {
      setState("unsupported");
      return;
    }

    const check = async () => {
      const permission = Notification.permission;
      if (permission === "denied") {
        setState("denied");
        return;
      }

      const reg = await navigator.serviceWorker.getRegistration("/sw.js");
      if (!reg) {
        setState("prompt");
        return;
      }

      const sub = await reg.pushManager.getSubscription();
      setState(sub ? "subscribed" : "prompt");
    };

    check();
  }, []);

  const subscribe = useCallback(async () => {
    setState("loading");
    try {
      // Register SW if not already
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;

      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY).buffer as ArrayBuffer,
      });

      const key = sub.getKey("p256dh");
      const auth = sub.getKey("auth");
      if (!key || !auth) throw new Error("Missing push keys");

      const p256dh = btoa(String.fromCharCode(...new Uint8Array(key)));
      const authStr = btoa(String.fromCharCode(...new Uint8Array(auth)));

      // Send to API
      const res = await fetch(`${API_BASE_URL}/api/v1/push-subscriptions`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          endpoint: sub.endpoint,
          p256dh,
          auth: authStr,
        }),
      });

      if (!res.ok) throw new Error(`Subscribe failed: ${res.status}`);
      setState("subscribed");
    } catch (err) {
      // Permission denied by user
      if (Notification.permission === "denied") {
        setState("denied");
      } else {
        setState("prompt");
      }
      console.error("Push subscribe error:", err);
    }
  }, []);

  const unsubscribe = useCallback(async () => {
    setState("loading");
    try {
      const reg = await navigator.serviceWorker.getRegistration("/sw.js");
      const sub = reg ? await reg.pushManager.getSubscription() : null;
      if (sub) {
        // Tell API first
        await fetch(`${API_BASE_URL}/api/v1/push-subscriptions`, {
          method: "DELETE",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        });
        await sub.unsubscribe();
      }
      setState("prompt");
    } catch (err) {
      setState("prompt");
      console.error("Push unsubscribe error:", err);
    }
  }, []);

  return { state, subscribe, unsubscribe };
}
