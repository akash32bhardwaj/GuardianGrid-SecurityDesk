/* ============================================================================
   Defender Octa — service worker (minimal, safe-by-default)
   Location: guardiangrid-command-center/public/sw.js  →  served at /frontend/sw.js

   Policy:
   - STATIC (js/css/icons/fonts): cache-first — instant loads, offline shell
   - API + video streams: NETWORK ONLY, never cached — a security dashboard
     must never show stale events, scores, or camera frames from cache
   - New deploys: bump CACHE_VERSION to invalidate old assets
   ========================================================================== */

const CACHE_VERSION = "octa-v2";   // bumped: navigation policy fix
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

  // NAVIGATIONS + index.html: NETWORK-FIRST. This is the fix that ends
  // stale builds: index.html references the hashed JS/CSS names, so if
  // it comes from cache, the ENTIRE old app loads. Fresh from network
  // every time; cache only as the offline fallback.
  const isNavigation =
    e.request.mode === "navigate" ||
    url.pathname === "/frontend/" ||
    url.pathname.endsWith("/index.html");
  if (isNavigation) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE_VERSION).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => caches.match(e.request)) // offline → last known shell
    );
    return;
  }

  // Hashed static assets under /frontend/assets/: cache-first is SAFE —
  // the filename changes with every build, so a cached copy can never be
  // stale. Other /frontend/ statics (manifest, icons): cache-first with
  // background refresh, as before.
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
