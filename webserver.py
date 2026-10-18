"""Веб-сервер внутри бота: отдаёт мини-приложение (webapp/) и запасной путь к OpenDota (/api/…).

Нужен, потому что из России Cloudflare (workers.dev) душат, а сервер бота доступен. Запросы к OpenDota
страница сначала делает сама, со своего устройства; /api/ — запасной путь через IP сервера бота.
"""
import logging
import time

import aiohttp
from aiohttp import web

from api import OPENDOTA, Api
from config import BASE_DIR, OPENDOTA_API_KEY

log = logging.getLogger(__name__)

INDEX = BASE_DIR / "webapp" / "index.html"
CACHE_MAX = 500   # ответов OpenDota в памяти


def make_app(api: Api) -> web.Application:
    cache: dict[str, tuple[float, int, bytes]] = {}   # адрес -> (когда, статус, тело)

    async def index(_):
        return web.FileResponse(INDEX, headers={"Cache-Control": "no-cache"})

    async def proxy(request: web.Request):
        tail = request.match_info["tail"]
        ttl = 86400 if tail.startswith("constants/") else 60
        params = list(request.query.items())
        key = tail + "?" + request.query_string
        hit = cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            status, body = hit[1], hit[2]
        else:
            if OPENDOTA_API_KEY:
                params.append(("api_key", OPENDOTA_API_KEY))
            try:
                async with api.session.get(f"{OPENDOTA}/{tail}", params=params,
                                           timeout=aiohttp.ClientTimeout(total=25)) as r:
                    status, body = r.status, await r.read()
            except (aiohttp.ClientError, TimeoutError) as e:
                log.info("webapp proxy %s: %s", tail, e)
                return web.json_response({"error": "OpenDota недоступен"}, status=502)
            if status == 200:
                if len(cache) >= CACHE_MAX:   # выкидываем самые старые
                    for k in sorted(cache, key=lambda k: cache[k][0])[:CACHE_MAX // 5]:
                        del cache[k]
                cache[key] = (time.time(), status, body)
        return web.Response(body=body, status=status, content_type="application/json",
                            headers={"Cache-Control": f"public, max-age={ttl}", "Access-Control-Allow-Origin": "*"})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/api/{tail:.+}", proxy)
    return app


async def start_web(api: Api, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(api), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("webapp server on port %d", port)
    return runner
