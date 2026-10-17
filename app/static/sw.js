/* 离线外壳缓存：只缓存界面骨架，不缓存业务数据。

   目的是让手机把主屏图标点开时立刻看到界面，不是让它在离线时读到旧事项。
   任何 /api/ 请求都不进缓存，也不拦截，避免演示时看到过期内容。
*/
const CACHE = "pa-shell-v1";
const SHELL = ["/", "/static/manifest.webmanifest", "/static/icon-192.png",
               "/static/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => {
      const stale = keys.filter((k) => k !== CACHE);
      return Promise.all(stale.map((k) => caches.delete(k)))
        .then(() => stale.length > 0);
    }).then((updated) => self.clients.claim().then(() => updated))
      .then((updated) => {
        // 界面换版了要让开着的页面也换掉。没有这一步时，用户手机上
        // 那个标签页会一直跑旧代码：预设 prompt 照旧把句子塞回输入框、
        // 照旧发完还挂在横栏上，而他看到的服务端明明已经是新的。
        // 他没有任何理由怀疑是缓存，只会觉得「这东西时好时坏」。
        // 只在自己替换掉了旧版本时才发一次，避免每次激活都刷屏。
        if (!updated) return;
        return self.clients.matchAll({ includeControlled: true })
          .then((cs) => {
            for (const c of cs) {
              c.postMessage({ type: "pa-sw-updated" });
            }
          });
      })
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // 业务数据一律走网络：缓存里的旧事项比没有界面更糟。
  if (url.pathname.startsWith("/api/")) return;
  e.respondWith(fetch(req).then((res) => {
    const copy = res.clone();
    caches.open(CACHE).then((c) => c.put(req, copy));
    return res;
  }).catch(() => caches.match(req).then((hit) => hit || caches.match("/"))));
});
