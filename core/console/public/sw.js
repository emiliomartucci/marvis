// Service Worker for Web Push Notifications
// v1.2.0 - 2026-04-20 - Add try/catch on push event.data.json() for non-JSON payloads

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  if (!event.data) return;

  let data;
  try {
    data = event.data.json();
  } catch {
    // Non-JSON payload (es. plain text "Test push" dal test button): fallback
    data = { title: "PiR Console", body: event.data.text() };
  }

  // Suppress notification if app tab is focused (dedup)
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: false }).then((clients) => {
      const hasFocused = clients.some((c) => c.visibilityState === "visible");
      if (hasFocused) return;

      return self.registration.showNotification(data.title || "PiR Console", {
        body: data.body || "",
        icon: "/favicon.svg",
        tag: data.type || "default",
        data: { type: data.type },
      });
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      // Focus existing tab if available
      for (const client of clients) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          return client.focus();
        }
      }
      // Otherwise open new tab
      return self.clients.openWindow("/");
    })
  );
});
