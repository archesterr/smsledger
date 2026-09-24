// Service worker: makes the app installable and shows an offline page. It caches only static
// assets and the offline page, never pages with financial data.
const CACHE = "smsledger-{{ version }}";
const ASSETS = [{% for a in assets %}"{{ a|escapejs }}"{% if not forloop.last %}, {% endif %}{% endfor %}];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => Promise.all([
      c.addAll(ASSETS),
      // without cookies, so the cached page never contains anything user-specific
      fetch("/offline/", { credentials: "omit" }).then((r) => c.put("/offline/", r)),
    ])).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match("/offline/")));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    // bank logos etc. are cached on first use (names are content-hashed, so a hit is never stale)
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((r) => {
      if (r.ok && /\.[0-9a-f]{12}\.\w+$/.test(url.pathname)) {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return r;
    })));
  }
});
