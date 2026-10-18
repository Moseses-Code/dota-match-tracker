"""Сборка и отправка отчёта о матче."""
import logging
import re
from html import unescape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from analysis import apply_stratz_bans, apply_stratz_positions, mmr_to_rank_tier, parse_match
from api import Api
from averages import get_averages
from config import MMR_STEP
from db import DB
from details import build_details
from draft_image import build_draft_image
from emoji import strip_custom
from report import MmrInfo, build_report, short_caption

log = logging.getLogger(__name__)

# паки собственных эмодзи (custom_packs.Packs); задаётся в main.py
PACKS = None


def match_keyboard(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="OpenDota", url=f"https://www.opendota.com/matches/{match_id}"),
        InlineKeyboardButton(text="STRATZ", url=f"https://stratz.com/matches/{match_id}"),
        InlineKeyboardButton(text="Dotabuff", url=f"https://www.dotabuff.com/matches/{match_id}"),
    ], [InlineKeyboardButton(text="📈 Подробнее", callback_data=f"more:{match_id}")]])


async def _load(api: Api, data: dict, user, player_slot: int | None = None):
    """Матч с позициями и банами + сам игрок (или None, если его нет в матче)."""
    m = parse_match(data, api)
    await apply_stratz_positions(m, api)
    await apply_stratz_bans(m, api)
    me = m.find(user["account_id"])
    if me is None and player_slot is not None:   # если матчи скрыты, account_id может не прийти
        me = next((p for p in m.players if p.slot == player_slot), None)
    if me is not None:
        me.account_id = user["account_id"]
    return m, me


async def make_details(api: Api, user, match_id: int):
    """Подробная статистика: (png, caption), "unparsed" — матч ещё не разобран, None — нет игрока."""
    data = await api.match(match_id)
    if not data:
        return None
    if not data.get("version"):
        await api.request_parse(match_id)
        return "unparsed"
    m, me = await _load(api, data, user)
    if me is None:
        return None
    return await build_details(data, m, me, api)


async def make_report(api: Api, db: DB, user, match_id: int, player_slot: int | None = None,
                      data: dict | None = None, count_mmr: bool = True):
    """Возвращает (png, html) или None, если игрока нет в матче."""
    data = data or await api.match(match_id)
    if not data:
        return None
    m, me = await _load(api, data, user, player_slot)
    if me is None:
        return None

    # MMR: если игрок указал свой MMR — ведём счёт и ранг считаем по нему
    mmr = MmrInfo(ranked=m.is_ranked, old_rank=user["rank_tier"])
    if m.is_ranked:
        won = me.is_radiant == m.radiant_win
        mmr.delta = MMR_STEP if won else -MMR_STEP
    if user["mmr"]:
        mmr.current = user["mmr"] + (mmr.delta if (count_mmr and mmr.delta) else 0)
        new_rank = mmr_to_rank_tier(mmr.current)
        mmr.old_rank = mmr_to_rank_tier(user["mmr"])
    else:
        # медаль из самого матча свежее, чем в профиле OpenDota
        profile = {} if me.rank_tier else (await api.player(user["account_id"]) or {})
        new_rank = me.rank_tier or profile.get("rank_tier")
        mmr.leaderboard = profile.get("leaderboard_rank")
    mmr.new_rank = new_rank
    if count_mmr:
        await db.update_user(user["tg_id"], rank_tier=new_rank, mmr=mmr.current or user["mmr"])
    else:
        mmr.old_rank = None   # для ручного запроса изменение ранга не показываем

    avg = await get_averages(api, db, me.hero_id, me.position, new_rank or m.avg_rank_tier, m.duration)
    if PACKS:
        await PACKS.ensure_for_report(me.hero_id, me.items + [me.neutral, me.enhancement], new_rank)
    html = build_report(m, me, avg, mmr, api)
    png = await build_draft_image(m, me.account_id, api)
    return png, html, short_caption(m, me), m.match_id


async def send_report(bot: Bot, chat_id: int, png: bytes, html: str, caption: str, match_id: int):
    photo = BufferedInputFile(png, filename=f"match_{match_id}.png")
    kb = match_keyboard(match_id)

    async def safe(send, text, **kw):
        try:
            await send(chat_id, text, **kw)
        except TelegramBadRequest as e:
            # например, премиум-эмодзи недоступны (у владельца нет Premium) — шлём с обычными
            log.warning("send failed (%s), retrying without custom emoji", e)
            await send(chat_id, strip_custom(text), **kw)

    # одно сообщение: картинка драфта + отчёт подписью (лимит подписи — 1024 символа)
    if _visible_len(html) <= 1024:
        await safe(lambda c, t, **kw: bot.send_photo(c, photo, caption=t, **kw), html, reply_markup=kb)
    else:
        await safe(lambda c, t, **kw: bot.send_photo(c, photo, caption=t, **kw), caption)
        await safe(bot.send_message, html, reply_markup=kb)


def _visible_len(html: str) -> int:
    return len(unescape(re.sub(r"<[^>]+>", "", html)).encode("utf-16-le")) // 2
