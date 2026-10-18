// Cloudflare Worker: статичные файлы из webapp/ + посредник к OpenDota по адресу /api/*.
// Посредник нужен, потому что из части сетей api.opendota.com недоступен, а Cloudflare — доступен.
// Ответы кэшируются на краю Cloudflare: константы игры — на сутки, данные игрока — на минуту.
const OPENDOTA = "https://api.opendota.com/api";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);
    if (request.method !== "GET") return new Response("Method not allowed", { status: 405 });

    const target = OPENDOTA + url.pathname.slice(4) + url.search;
    const ttl = url.pathname.startsWith("/api/constants/") ? 86400 : 60;
    try {
      const r = await fetch(target, { cf: { cacheTtl: ttl, cacheEverything: true }, headers: { "User-Agent": "dota-match-tracker" } });
      const headers = new Headers(r.headers);
      headers.set("Access-Control-Allow-Origin", "*");
      headers.set("Cache-Control", `public, max-age=${ttl}`);
      return new Response(r.body, { status: r.status, headers });
    } catch (e) {
      return new Response(JSON.stringify({ error: "OpenDota недоступен" }), { status: 502, headers: { "Content-Type": "application/json" } });
    }
  },
};
