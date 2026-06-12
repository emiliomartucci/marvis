// Marvis Console PWA service worker.

const SCOPE_PATH = "/ui/";
const CACHE_VERSION = new URL(self.location.href).searchParams.get("v") || "static";
const CACHE_NAME = `marvis-console-${CACHE_VERSION}`;
const HASHED_ASSET_RE = /\/ui\/_next\/static\/|\/ui\/icons\/|\/ui\/fonts\/|\/ui\/favicon/;

function isUiRequest(request) {
  const url = new URL(request.url);
  return url.origin === self.location.origin && url.pathname.startsWith(SCOPE_PATH);
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    await cache.put(request, response.clone());
  }
  return response;
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response.ok) {
      await cache.put(request, response.clone());
    }
    return response;
  } catch {
    return (await cache.match(request)) ?? caches.match(`${SCOPE_PATH}index.html`);
  }
}

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("marvis-console-") && key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET" || !isUiRequest(event.request)) return;

  const url = new URL(event.request.url);
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirst(event.request));
    return;
  }

  if (HASHED_ASSET_RE.test(url.pathname)) {
    event.respondWith(cacheFirst(event.request));
  }
});

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

      return self.registration.showNotification(data.title || "Marvis Console", {
        body: data.body || "",
        icon: `${SCOPE_PATH}icons/icon-192.png`,
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
      return self.clients.openWindow(SCOPE_PATH);
    })
  );
});
