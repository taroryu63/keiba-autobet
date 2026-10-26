// 画面の部品だけをキャッシュする（APIの結果はキャッシュしない）
const CACHE = "keiba-v1";
const SHELL = ["/", "/index.html", "/style.css", "/app.js", "/icon.svg", "/manifest.webmanifest"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL))));
self.addEventListener("activate", (e) =>
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))))
);
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  // 新しい版を優先し、つながらないときだけキャッシュを使う
  e.respondWith(fetch(e.request).then((r) => {
    const copy = r.clone();
    caches.open(CACHE).then((c) => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request)));
});
