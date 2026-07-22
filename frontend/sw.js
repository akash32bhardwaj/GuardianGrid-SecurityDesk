/* ============================================================================
   Defender Octa — service worker (minimal, safe-by-default)
   Location: guardiangrid-command-center/public/sw.js  →  served at /frontend/sw.js

   Policy:
   - STATIC (js/css/icons/fonts): cache-first — instant loads, offline shell
   - API + video streams: NETWORK ONLY, never cached — a security dashboard
     must never show stale events, scores, or camera frames from cache
   - New deploys: bump CACHE_VERSION to invalidate old assets
   ========================================================================== */

const CACHE_VERSION = "octa-v1";
const APP_SHELL = ["/frontend/", "/frontend/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE_VERSION).then((c) => c.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Never touch: API calls, auth, MJPEG/video streams, recordings, non-GET
  if (
    e.request.method !== "GET" ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/video_feed") ||
    url.pathname.startsWith("/cam/") ||
    url.pathname.includes("/replay/") ||
    url.pathname.startsWith("/gate/") ||
    url.pathname.startsWith("/alerts") ||
    url.pathname.startsWith("/notifications") ||
    url.pathname.startsWith("/activity_feed") ||
    url.pathname.startsWith("/vehicle") ||
    url.pathname.startsWith("/residents") ||
    url.pathname.startsWith("/camera_heat") ||
    url.pathname.startsWith("/hourly_stats") ||
    url.pathname.startsWith("/visitors")
  ) {
    return; // fall through to network untouched
  }

  // Static assets under /frontend/: cache-first with background refresh
  if (url.pathname.startsWith("/frontend/")) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        const fetched = fetch(e.request)
          .then((res) => {
            if (res.ok) {
              const copy = res.clone();
              caches.open(CACHE_VERSION).then((c) => c.put(e.request, copy));
            }
            return res;
          })
          .catch(() => cached); // offline → serve shell
        return cached || fetched;
      })
    );
  }
});
