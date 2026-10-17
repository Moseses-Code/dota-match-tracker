"""Собственные паки премиум-эмодзи бота: герои, предметы, медали рангов.

Бот сам создаёт паки из официальных иконок Dota 2 (Steam CDN / OpenDota) и запоминает ID эмодзи.
Паки принадлежат владельцу бота (OWNER_ID), он должен хотя бы раз нажать /start.
В одном паке до 200 эмодзи, поэтому предметы раскладываются по нескольким пакам.
"""
import asyncio
import io
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputSticker
from PIL import Image, ImageDraw, ImageOps

import emoji
from api import Api
from config import BASE_DIR, OWNER_ID
from db import DB

log = logging.getLogger(__name__)

SIZE = 100
MAP_FILE = BASE_DIR / "emoji_map.json"
PACK_LIMIT = 200
RANK_URL = "https://www.opendota.com/assets/images/dota2/rank_icons/"
CDN = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/"
# тематические иконки оформления: ключ emoji.e() -> картинка из Dota 2
OD = "https://www.opendota.com/assets/images/"
# ключ -> (url, эмодзи-заглушка, как вписать: "item" — целиком, "crop" — квадрат из центра)
UI_ICONS = {
    "win": (CDN + "items/aegis.png", "🏆", "item"),
    "kda": (CDN + "items/rapier.png", "⚔️", "item"),
    "gpm": (CDN + "icons/gold.png", "💰", "item"),
    "xpm": (CDN + "items/tome_of_knowledge.png", "📚", "item"),
    "creeps": (CDN + "items/quelling_blade.png", "🗡", "item"),
    "nw": (CDN + "items/hand_of_midas.png", "💎", "item"),
    "radiant": (OD + "radiant.png", "🌳", "crop"),   # древний Сил Света
    "dire": (OD + "dire.png", "🌋", "crop"),         # древний Сил Тьмы
    "fav": (CDN + "items/heart.png", "❤️", "item"),
    "matches": (CDN + "items/ward_observer.png", "👁", "item"),
}


def _rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius, fill=255)
    img.putalpha(mask)
    return img


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def hero_icon(data: bytes) -> bytes:
    """Квадрат из центра портрета (лицо героя), скруглённые углы."""
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img = ImageOps.fit(img, (SIZE, SIZE), Image.LANCZOS, centering=(0.5, 0.35))
    return _png(_rounded(img, 22))


def faction_icon(data: bytes) -> bytes:
    """Квадрат из центра картинки фракции (силуэт древнего)."""
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img = ImageOps.fit(img, (SIZE, SIZE), Image.LANCZOS, centering=(0.5, 0.7))
    return _png(_rounded(img, 50))


def item_icon(data: bytes) -> bytes:
    """Иконка предмета (88x64) по центру прозрачного квадрата."""
    src = Image.open(io.BytesIO(data)).convert("RGBA")
    src = ImageOps.contain(src, (SIZE, SIZE), Image.LANCZOS)
    src = _rounded(src, 12)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(src, ((SIZE - src.width) // 2, (SIZE - src.height) // 2), src)
    return _png(canvas)


def rank_icon(medal: bytes, star: bytes | None) -> bytes:
    img = Image.open(io.BytesIO(medal)).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
    if star:
        s = Image.open(io.BytesIO(star)).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
        img.alpha_composite(s)
    return _png(img)


class Packs:
    def __init__(self, bot: Bot, api: Api, db: DB):
        self.bot, self.api, self.db = bot, api, db
        self.lock = asyncio.Lock()
        self.username = ""
        self._sets: dict[str, int] = {}   # имя пака -> сколько эмодзи в нём

    @property
    def enabled(self):
        return bool(OWNER_ID)

    async def load(self):
        self.username = (await self.bot.get_me()).username
        # ID уже загруженных эмодзи хранятся и в emoji_map.json (в репозитории), чтобы на новом
        # сервере бот не загружал всё заново и не плодил дубли в паках
        if MAP_FILE.exists():
            for key, cid in json.loads(MAP_FILE.read_text("utf-8")).items():
                await self.db.set_emoji(key, cid)
        MAP_FILE.write_text(json.dumps(dict(sorted(emoji.dynamic.items())), indent=0), "utf-8")
        emoji.dynamic.update(await self.db.all_emoji())
        log.info("custom emoji loaded: %d", len(emoji.dynamic))

    def _name(self, kind: str, n: int) -> str:
        return f"dota_{kind}{n}_by_{self.username}".lower()

    async def _set_size(self, name: str) -> int | None:
        if name in self._sets:
            return self._sets[name]
        try:
            s = await self.bot.get_sticker_set(name)
            self._sets[name] = len(s.stickers)
        except TelegramBadRequest:
            return None
        return self._sets[name]

    async def _upload(self, kind: str, title: str, png: bytes, char: str) -> str | None:
        sticker = InputSticker(sticker=BufferedInputFile(png, "e.png"), format="static", emoji_list=[char])
        n = 1
        while True:
            name = self._name(kind, n)
            size = await self._set_size(name)
            if size is None:   # пака ещё нет — создаём
                await self.bot.create_new_sticker_set(
                    user_id=OWNER_ID, name=name, title=f"{title} #{n}" if n > 1 else title,
                    stickers=[sticker], sticker_type="custom_emoji")
                break
            if size < PACK_LIMIT:
                await self.bot.add_sticker_to_set(user_id=OWNER_ID, name=name, sticker=sticker)
                break
            n += 1
        s = await self.bot.get_sticker_set(name)
        self._sets[name] = len(s.stickers)
        return s.stickers[-1].custom_emoji_id

    async def _ensure(self, key: str, kind: str, title: str, make, char: str) -> bool:
        if key in emoji.dynamic:
            return True
        png = await make()
        if not png:
            return False
        for attempt in range(3):
            try:
                cid = await self._upload(kind, title, png, char)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramBadRequest as e:
                log.warning("emoji %s upload failed: %s", key, e)
                return False
        else:
            return False
        emoji.dynamic[key] = cid
        await self.db.set_emoji(key, cid)
        return True

    # --- публичные методы ---
    async def ensure_hero(self, hero_id: int):
        async def make():
            data = await self.api.hero_image(hero_id)
            return hero_icon(data) if data else None
        async with self.lock:
            return await self._ensure(f"hero:{hero_id}", "heroes", "Dota 2 Heroes", make, "🦸")

    async def ensure_item(self, item_id: int):
        it = self.api.items.get(item_id)
        if not it:
            return False

        async def make():
            data = await self.api.item_image(item_id)
            return item_icon(data) if data else None
        async with self.lock:
            return await self._ensure(f"item:{it['name']}", "items", "Dota 2 Items", make, "🗡")

    def all_item_ids(self) -> list[int]:
        """Все предметы игры с иконкой, кроме рецептов."""
        return sorted(i for i, it in self.api.items.items()
                      if it.get("img") and not it["name"].startswith("recipe_"))

    async def ensure_rank(self, rank_tier: int):
        medal, stars = divmod(int(rank_tier), 10)

        async def make():
            m = await self.api.fetch_bytes(f"{RANK_URL}rank_icon_{medal}.png")
            s = await self.api.fetch_bytes(f"{RANK_URL}rank_star_{stars}.png") if 0 < stars <= 5 and medal < 8 else None
            return rank_icon(m, s) if m else None
        async with self.lock:
            return await self._ensure(f"rank:{rank_tier}", "ranks", "Dota 2 Ranks", make, "🏅")

    async def ensure_ui(self):
        for key, (url, char, fit) in UI_ICONS.items():
            async def make(url=url, fit=fit):
                data = await self.api.fetch_bytes(url)
                if not data:
                    return None
                return item_icon(data) if fit == "item" else faction_icon(data)
            async with self.lock:
                await self._ensure(f"ui:{key}", "ui", "Dota 2 Stats UI", make, char)

    async def ensure_for_report(self, hero_id: int, item_ids: list[int], rank_tier: int | None):
        """Догружает иконки, нужные для конкретного отчёта (обычно уже есть)."""
        if not self.enabled:
            return
        try:
            await self.ensure_ui()
            await self.ensure_hero(hero_id)
            for i in item_ids:
                if i:
                    await self.ensure_item(i)
            if rank_tier:
                await self.ensure_rank(rank_tier)
        except Exception:  # noqa: BLE001
            log.exception("custom emoji")

    async def prewarm(self):
        """В фоне загружает всех героев и все медали, чтобы первые отчёты были быстрыми."""
        if not self.enabled:
            log.info("OWNER_ID не задан — собственные эмодзи (герои/предметы/ранги) отключены")
            return
        try:
            await self.ensure_ui()
            for tier in [m * 10 + s for m in range(1, 8) for s in range(1, 6)] + [80]:
                await self.ensure_rank(tier)
                await asyncio.sleep(0.5)
            for hero_id in sorted(self.api.heroes):
                await self.ensure_hero(hero_id)
                await asyncio.sleep(0.5)
            for item_id in self.all_item_ids():
                await self.ensure_item(item_id)
                await asyncio.sleep(0.5)
            log.info("custom emoji prewarm done: %d", len(emoji.dynamic))
        except Exception:  # noqa: BLE001
            log.exception("prewarm failed")
