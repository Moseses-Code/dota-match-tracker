"""Клиенты OpenDota (REST) и STRATZ (GraphQL)."""
import asyncio
import contextvars
import json
import logging
import time

import aiohttp

from config import CACHE_DIR, OPENDOTA_API_KEY, STRATZ_TOKEN

log = logging.getLogger(__name__)

OPENDOTA = "https://api.opendota.com/api"
STRATZ = "https://api.stratz.com/graphql"
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com"
REFRESH_EVERY = 300   # не чаще раза в 5 минут просим OpenDota обновить историю игрока
BG_OPENDOTA_EVERY = 600   # фоновая проверка спрашивает OpenDota не чаще раза в 10 минут на игрока
# True внутри фоновых задач (проверка матчей): их запросы уступают очередь командам пользователей
BACKGROUND = contextvars.ContextVar("background", default=False)


class RateLimiter:
    """Не чаще одного запроса в `interval` секунд. Фоновые запросы пропускают вперёд
    запросы пользователей: пока кто-то из пользователей ждёт очереди, фон не встаёт в неё."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._waiting = 0   # запросов пользователей в очереди

    async def wait(self):
        background = BACKGROUND.get()
        if not background:
            self._waiting += 1
        try:
            while True:
                async with self._lock:
                    # фон, дошедший до своей очереди, уступает её, если ждёт пользователь
                    if not (background and self._waiting):
                        delay = self._last + self.interval - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._last = time.monotonic()
                        return
                await asyncio.sleep(0.05)
        finally:
            if not background:
                self._waiting -= 1


class ApiError(Exception):
    pass


class Api:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.od_limit = RateLimiter(1.1)   # бесплатный OpenDota: 60 запросов/мин
        self.sz_limit = RateLimiter(0.3)
        self.heroes: dict[int, dict] = {}
        self.items: dict[int, dict] = {}   # id -> {name, dname, img, cost}
        self._refreshed: dict[int, float] = {}
        self._od_checked: dict[int, float] = {}   # когда фоновая проверка последний раз спрашивала OpenDota
        self._tasks: set[asyncio.Task] = set()     # фоновые задачи (ссылка, чтобы их не собрал сборщик мусора)

    async def open(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        await self.load_constants()

    async def close(self):
        if self.session:
            await self.session.close()

    # ---------- OpenDota ----------
    async def od(self, path, method="GET", params=None):
        # список пар — когда параметр повторяется (project=…&project=…)
        params = list(params.items() if isinstance(params, dict) else params or [])
        if OPENDOTA_API_KEY:
            params.append(("api_key", OPENDOTA_API_KEY))
        for attempt in range(3):
            await self.od_limit.wait()
            try:
                async with self.session.request(method, OPENDOTA + path, params=params) as r:
                    if r.status == 429:
                        await asyncio.sleep(10 * (attempt + 1))
                        continue
                    if r.status == 404:
                        return None
                    if r.status >= 400:
                        raise ApiError(f"OpenDota {path}: HTTP {r.status}")
                    return await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("OpenDota %s: %s", path, e)
                await asyncio.sleep(3)
        raise ApiError(f"OpenDota {path}: нет ответа")

    async def player(self, account_id):
        return await self.od(f"/players/{account_id}")

    async def recent_matches(self, account_id):
        return await self.od(f"/players/{account_id}/recentMatches") or []

    async def match_history(self, account_id, limit=10, offset=0) -> list[dict]:
        """Матчи игрока, новые сверху: герой, K/D/A, длительность, результат."""
        return await self.od(f"/players/{int(account_id)}/matches",
                             params={"limit": limit, "offset": offset}) or []

    async def peers(self, account_id, days=30) -> list[dict]:
        """С кем игрок играл за последние `days` дней (только открытые профили)."""
        return await self.od(f"/players/{int(account_id)}/peers", params={"date": days}) or []

    async def stratz_recent(self, account_id) -> list[dict]:
        """Последние матчи из STRATZ (видит открытую историю сразу, OpenDota — с задержкой)."""
        data = await self.stratz(
            f"{{ player(steamAccountId: {int(account_id)}) {{ matches(request: {{take: 5}}) {{"
            f" id startDateTime durationSeconds players(steamAccountId: {int(account_id)}) {{ playerSlot }} }} }} }}"
        )
        out = []
        for m in ((data or {}).get("player") or {}).get("matches") or []:
            players = m.get("players") or [{}]
            out.append({"match_id": m["id"], "player_slot": players[0].get("playerSlot"),
                        "start_time": m.get("startDateTime") or 0, "duration": m.get("durationSeconds") or 0})
        return out

    async def profile_stats(self, account_id) -> dict:
        """Имя, аватар, всего матчей, побед и топ героев (STRATZ, запасной вариант — OpenDota)."""
        acc = int(account_id)
        if self.has_stratz:
            try:
                data = await self.stratz(
                    f"{{ player(steamAccountId: {acc}) {{ matchCount winCount steamAccount {{ name avatar }}"
                    f" heroesPerformance(request: {{take: 100000}}, take: 200) {{ heroId matchCount winCount }} }} }}")
                p = data.get("player") or {}
                heroes = sorted(p.get("heroesPerformance") or [], key=lambda h: -h["matchCount"])
                return {"name": (p.get("steamAccount") or {}).get("name"),
                        "avatar": (p.get("steamAccount") or {}).get("avatar"),
                        "matches": p.get("matchCount") or 0, "wins": p.get("winCount") or 0,
                        "heroes": [(h["heroId"], h["matchCount"], h["winCount"]) for h in heroes[:3]]}
            except ApiError as e:
                log.info("STRATZ profile: %s", e)
        player = await self.player(acc) or {}
        wl = await self.od(f"/players/{acc}/wl") or {}
        heroes = await self.od(f"/players/{acc}/heroes") or []
        prof = player.get("profile") or {}
        return {"name": prof.get("personaname"), "avatar": prof.get("avatarfull"),
                "matches": (wl.get("win") or 0) + (wl.get("lose") or 0), "wins": wl.get("win") or 0,
                "heroes": [(int(h["hero_id"]), h["games"], h["win"]) for h in heroes[:3] if h.get("games")]}

    async def refresh_player(self, account_id):
        """Просит OpenDota подтянуть свежую историю из Steam (иначе recentMatches может отставать)."""
        acc = int(account_id)
        if time.monotonic() - self._refreshed.get(acc, -REFRESH_EVERY) < REFRESH_EVERY:
            return
        self._refreshed[acc] = time.monotonic()
        try:
            await self.od(f"/players/{acc}/refresh", method="POST")
        except ApiError as e:
            log.info("refresh %s failed: %s", acc, e)

    async def latest_matches(self, account_id, known_last: int | None = None) -> list[dict]:
        """Последние матчи игрока, новые сверху: объединяем STRATZ и OpenDota.

        Любой из источников может отставать на несколько матчей, поэтому берём оба,
        а не только первый ответивший — иначе новый матч не виден, пока его не увидит STRATZ.
        known_last — для фоновой проверки: если STRATZ уже показал матч новее, OpenDota не спрашиваем,
        а если нет — спрашиваем не чаще раза в BG_OPENDOTA_EVERY секунд, чтобы не забивать очередь.
        """
        acc = int(account_id)
        found: dict[int, dict] = {}
        if self.has_stratz:
            try:
                for r in await self.stratz_recent(account_id):
                    found[r["match_id"]] = r
            except ApiError as e:
                log.info("STRATZ recent: %s", e)
        if known_last is not None and found:
            fresh = max(found) > known_last
            if fresh or time.monotonic() - self._od_checked.get(acc, -BG_OPENDOTA_EVERY) < BG_OPENDOTA_EVERY:
                return sorted(found.values(), key=lambda r: r["match_id"], reverse=True)
        if known_last is not None:
            self._od_checked[acc] = time.monotonic()
        # просьба обновить историю — в фоне: пользователь её не ждёт, она пригодится к следующему разу
        task = asyncio.create_task(self._refresh_background(acc))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            for r in await self.recent_matches(account_id):
                found.setdefault(r["match_id"], r)
        except ApiError as e:
            if not found:
                raise
            log.info("OpenDota recent: %s", e)
        return sorted(found.values(), key=lambda r: r["match_id"], reverse=True)

    async def _refresh_background(self, acc: int):
        BACKGROUND.set(True)
        await self.refresh_player(acc)

    async def match(self, match_id):
        return await self.od(f"/matches/{match_id}")

    async def request_parse(self, match_id):
        try:
            await self.od(f"/request/{match_id}", method="POST")
        except ApiError as e:
            log.info("parse request failed: %s", e)

    async def load_constants(self):
        heroes = await self._cached_json("heroes.json", "/constants/heroes")
        self.heroes = {int(h["id"]): h for h in heroes.values()}
        items = await self._cached_json("items.json", "/constants/items")
        self.items = {}
        for name, it in items.items():
            if "id" in it:
                self.items[int(it["id"])] = {"name": name, "dname": it.get("dname") or name, "img": it.get("img"),
                                            "cost": it.get("cost") or 0}

    async def _cached_json(self, filename, path, max_age=86400 * 3):
        f = CACHE_DIR / filename
        if f.exists() and time.time() - f.stat().st_mtime < max_age:
            return json.loads(f.read_text("utf-8"))
        try:
            data = await self.od(path)
            f.write_text(json.dumps(data), "utf-8")
            return data
        except ApiError:
            if f.exists():
                return json.loads(f.read_text("utf-8"))
            raise

    # ---------- STRATZ ----------
    @property
    def has_stratz(self):
        return bool(STRATZ_TOKEN)

    async def stratz(self, query: str):
        if not STRATZ_TOKEN:
            raise ApiError("STRATZ_TOKEN не задан")
        headers = {
            "Authorization": f"Bearer {STRATZ_TOKEN}",
            "User-Agent": "STRATZ_API",
            "Content-Type": "application/json",
        }
        for attempt in range(3):
            await self.sz_limit.wait()
            try:
                async with self.session.post(STRATZ, json={"query": query}, headers=headers) as r:
                    if r.status == 429:
                        await asyncio.sleep(10 * (attempt + 1))
                        continue
                    text = await r.text()
                    if r.status == 403:   # например, лимит «не больше 2 IP за 15 минут»
                        raise ApiError(f"STRATZ 403: {text[:200]}")
                    body = json.loads(text)
                    if r.status >= 400 or body.get("errors"):
                        raise ApiError(f"STRATZ: HTTP {r.status} {str(body.get('errors'))[:300]}")
                    return body["data"]
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                log.warning("STRATZ: %s", e)
                await asyncio.sleep(3)
        raise ApiError("STRATZ: нет ответа")

    # ---------- картинки ----------
    async def image(self, kind: str, key: str, url_path: str) -> bytes | None:
        """Скачивает и кэширует картинку героя/предмета со Steam CDN."""
        folder = CACHE_DIR / kind
        folder.mkdir(exist_ok=True)
        f = folder / f"{key}.png"
        if f.exists():
            return f.read_bytes()
        url = STEAM_CDN + url_path.split("?")[0]
        try:
            async with self.session.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.read()
                f.write_bytes(data)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def hero_image(self, hero_id: int):
        h = self.heroes.get(hero_id)
        if not h:
            return None
        return await self.image("heroes", str(hero_id), h["img"])

    async def item_image(self, item_id: int):
        it = self.items.get(item_id)
        if not it or not it.get("img"):
            return None
        return await self.image("items", it["name"], it["img"])

    async def fetch_bytes(self, url: str) -> bytes | None:
        try:
            async with self.session.get(url) as r:
                return await r.read() if r.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def download(self, url: str, dest):
        async with self.session.get(url) as r:
            r.raise_for_status()
            dest.write_bytes(await r.read())
