// Сервис-воркер нужен, чтобы сайт можно было установить как приложение. Данные он не кэширует:
// заказы всегда берутся с сервера, иначе водитель увидел бы устаревшую ленту.
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (event) { event.waitUntil(self.clients.claim()); });

var OFFLINE_HTML = '<!doctype html><html lang="ru"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Нет связи</title>' +
  '<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;' +
  'font-family:system-ui,sans-serif;background:#0b0f19;color:#e6e8ee;text-align:center;padding:24px}' +
  'button{margin-top:16px;padding:12px 20px;border:0;border-radius:10px;background:#1668f5;color:#fff;font-size:16px}' +
  '</style></head><body><div><h1>Нет связи</h1><p>Проверьте интернет: заказы загружаются с сервера.</p>' +
  '<button onclick="location.reload()">Повторить</button></div></body></html>';

self.addEventListener('fetch', function (event) {
  if (event.request.mode !== 'navigate') { return; }
  event.respondWith(fetch(event.request).catch(function () {
    return new Response(OFFLINE_HTML, { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }));
});
