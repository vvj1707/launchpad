const C="lp1";self.addEventListener("install",e=>e.waitUntil(caches.open(C).then(c=>c.addAll(["/"]))));
self.addEventListener("fetch",e=>{if(e.request.method!=="GET")return;e.respondWith(fetch(e.request).then(r=>{if(e.request.url.includes("/api/")===false){const k=r.clone();caches.open(C).then(c=>c.put(e.request,k))}return r}).catch(()=>caches.match(e.request)))});
