// Service worker: ให้เปิดแอปได้แม้ออฟไลน์ และได้ไฟล์เวอร์ชันใหม่เสมอเมื่อมีเน็ต
const CACHE = "pf-v3";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // เน็ตก่อน (ได้ของใหม่เสมอ) ถ้าไม่มีเน็ตค่อยใช้ของที่แคชไว้
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          // เก็บโดยตัดพารามิเตอร์ ?t=... ออก ไม่งั้นแคชจะโตขึ้นทุกครั้งที่กดรีเฟรช
          caches.open(CACHE).then((c) => c.put(req.url.split("?")[0], copy));
        }
        return res;
      })
      .catch(() =>
        caches.match(req, { ignoreSearch: true }).then((r) => r || caches.match("index.html"))
      )
  );
});
