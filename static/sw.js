// AXIS PWA Service Worker – Auto‑cache Student Profiles
const CACHE_NAME = 'axis-pwa-v5';
const STATIC_ASSETS = [
    '/manifest.json',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/static/js/offline_student.js',
    '/static/js/offline_payment.js'
];

// Refresh all student profiles by fetching the API and caching each profile
async function refreshStudentProfiles(schema) {
    if (!schema) return;
    const apiUrl = `/portal/${schema}/api/students/`;
    try {
        const res = await fetch(apiUrl, { cache: 'reload' });
        if (!res.ok) return;
        const students = await res.json();
        if (!students || students.length === 0) return;
        const urls = students.flatMap(s => [s.desktop_url, s.mobile_url]);
        await Promise.all(urls.map(url =>
            fetch(url, { cache: 'reload' })
                .then(resp => {
                    if (resp.ok) {
                        return caches.open(CACHE_NAME).then(cache => cache.put(url, resp));
                    }
                })
                .catch(() => {})
        ));
        console.log('[SW] Student profiles refreshed');
    } catch (e) {
        console.warn('[SW] Refresh failed:', e);
    }
}

// Generate offline student list page HTML
function getOfflineStudentListHTML(isMobile, pathname) {
    const parts = pathname.split('/');
    const schema = parts[2] || 'unknown';
    const baseStyle = `
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: system-ui, -apple-system, sans-serif; background: #f5f5f5; padding: 1rem; }
        .offline-container { max-width: 600px; margin: 0 auto; }
        .offline-header { background: #fff3cd; border: 1px solid #ffeaa7; padding: 1rem; border-radius: 0.5rem; margin-bottom: 1.5rem; }
        .offline-header h1 { font-size: 1.1rem; color: #856404; margin-bottom: 0.5rem; }
        .offline-header p { color: #856404; font-size: 0.9rem; }
        .student-list { padding: 0; }
        .student-card { background: #fff; border-radius: 0.75rem; padding: 1rem; margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-left: 4px solid #007bff; }
        .student-name { font-weight: 600; color: #333; margin-bottom: 0.5rem; }
        .student-meta { font-size: 0.85rem; color: #666; margin-bottom: 0.5rem; }
        .pending-badge { display: inline-block; background: #fef3c7; color: #92400e; padding: 0.25rem 0.75rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; margin-top: 0.5rem; }
    `;
    
    return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Students - Offline</title>
    <style>${baseStyle}</style>
</head>
<body>
    <div class="offline-container">
        <div class="offline-header">
            <h1>📡 You're Offline</h1>
            <p>Showing pending students from offline storage. Connect to sync changes.</p>
        </div>
        <div id="studentContainer" class="student-list"></div>
        <table class="data-table">
            <tbody></tbody>
        </table>
        <div id="noStudents" style="text-align: center; padding: 2rem; color: #999;">
            <p>No pending students yet</p>
            <p style="font-size: 0.85rem; margin-top: 0.5rem;">Create a student offline and it will appear here</p>
        </div>
    </div>
    <script>
        window.AXIS_SCHEMA = '${schema}';
    </script>
    <script src="/static/js/offline_student.js"></script>
</body>
</html>`;
}

// ---- Install ----
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(STATIC_ASSETS).catch(() => {}))
            .then(() => self.skipWaiting())
    );
});

// ---- Activate ----
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys => Promise.all(
            keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
        )).then(() => {
            // Start periodic refresh every 15 minutes
            self.clients.matchAll().then(clients => {
                if (clients.length > 0) {
                    const url = new URL(clients[0].url);
                    const parts = url.pathname.split('/');
                    if (parts.length >= 3 && parts[1] === 'portal') {
                        const schema = parts[2];
                        refreshStudentProfiles(schema);
                        setInterval(() => {
                            refreshStudentProfiles(schema);
                        }, 15 * 60 * 1000);
                    }
                }
            });
            return self.clients.claim();
        })
    );
});

// ---- Fetch ----
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    // ---- 1. Student list API -> cache all profiles ----
    if (url.pathname.endsWith('/api/students/')) {
        const parts = url.pathname.split('/');
        const schema = parts[2];
        event.respondWith(
            fetch(event.request, { cache: 'reload' })
                .then(response => {
                    const cloned = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, cloned));
                    if (schema) refreshStudentProfiles(schema);
                    return response;
                })
                .catch(() => caches.match(event.request))
        );
        return;
    }

    // ---- 2. Student profile pages (stale-while-revalidate) ----
    if (/^\/portal\/[^\/]+\/students\/\d+\/?$/.test(url.pathname) ||
        /^\/portal\/[^\/]+\/students\/\d+\/mobile\/?$/.test(url.pathname)) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                const fetchPromise = fetch(event.request, { cache: 'reload' })
                    .then(response => {
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, response.clone()));
                        return response;
                    })
                    .catch(() => {});
                if (cached) {
                    fetchPromise.then(() => {});
                    return cached;
                }
                return fetchPromise;
            })
        );
        return;
    }

    // ---- 3. Other cached pages (list, dashboard, etc.) ----
    const isCachedPage = /^\/portal\/[^\/]+\/dashboard\//.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/dashboard\/mobile\//.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/dashboard\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/students\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/students\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/defaulters\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/defaulters\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/reports\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/reports\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/structure\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/structure\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/vouchers\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/vouchers\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/logs\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/logs\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/stock\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/stock\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/stock\/product\/\d+\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/stock\/product\/\d+\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/collection\/\d+\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/collection\/mobile\/\d+\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/collection\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/collection\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/settings\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/fee\/settings\/mobile\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/settings\/?$/.test(url.pathname) ||
                         /^\/portal\/[^\/]+\/settings\/mobile\/?$/.test(url.pathname);

    if (isCachedPage) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                const fetchPromise = fetch(event.request)
                    .then(response => {
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, response.clone()));
                        return response;
                    })
                    .catch(() => {});
                if (cached) {
                    fetchPromise.then(() => {});
                    return cached;
                }
                return fetchPromise;
            })
        );
        return;
    }

    // ---- 4. Static assets (cache-first) ----
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request)
                .then(cached => cached || fetch(event.request).then(response => {
                    if (response.ok) {
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, response.clone()));
                    }
                    return response;
                }))
                .catch(() => caches.match(event.request))
        );
        return;
    }

    // ---- 5. Other API / portal ----
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/portal/') || url.pathname === '/') {
        event.respondWith(
            fetch(event.request)
                .then(response => {
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, response.clone()));
                    return response;
                })
                .catch(() => {
                    // Try to return cached version
                    return caches.match(event.request).then(cached => {
                        if (cached) return cached;
                        
                        // If this is a student list page and not cached, serve offline page with pending students
                        if (/^\/portal\/[^\/]+\/(students\/?|students\/mobile\/?)?$/.test(url.pathname)) {
                            const isMobile = url.pathname.includes('/mobile');
                            const html = getOfflineStudentListHTML(isMobile, url.pathname);
                            return new Response(html, {
                                status: 200,
                                headers: {'Content-Type': 'text/html; charset=utf-8'}
                            });
                        }
                        
                        // Return nothing for other pages when offline and not cached
                        return undefined;
                    });
                })
        );
        return;
    }

    // ---- 6. Fallback ----
    event.respondWith(
        caches.match(event.request)
            .then(cached => cached || fetch(event.request).then(response => {
                if (response.ok) {
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, response.clone()));
                }
                return response;
            }))
    );
});
