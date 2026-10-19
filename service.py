"""Сборка и отправка отчёта о матче."""
import logging
import re
from html import unescape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo

from analysis import (apply_stratz, estimate_mmr, mmr_to_rank_tier, parse_match,
                      rank_tier_range)
from api import Api, ApiError
from averages import get_averages
from buttons import BLUE, ib
from config import MMR_STEP, WEBAPP_URL
from db import DB
from details import build_details
from draft_image import build_draft_image
from emoji import strip_custom
from report import MmrInfo, build_report, fmt_num, short_caption

log = logging.getLogger(__name__)

# паки собственных эмодзи (custom_packs.Packs); задаётся в main.py
PACKS = None


def match_keyboard(match_id: int, app_url: str | None = None) -> InlineKeyboardMarkup:
    second = [ib("Подробнее", "more", BLUE, callback_data=f"more:{match_id}")]
    if app_url:   # полный разбор этого матча в мини-приложении
        second.append(ib("В приложении", "last", web_app=WebAppInfo(url=f"{app_url}&match={match_id}")))
    return InlineKeyboardMarkup(inline_keyboard=[[
        ib("OpenDota", url=f"https://www.opendota.com/matches/{match_id}"),
        ib("STRATZ", url=f"https://stratz.com/matches/{match_id}"),
        ib("Dotabuff", url=f"https://www.dotabuff.com/matches/{match_id}"),
    ], second])


async def _load(api: Api, data: dict, user, player_slot: int | None = None):
    """Матч с позициями и банами + сам игрок (или None, если его нет в матче)."""
    m = parse_match(data, api)
    await apply_stratz(m, api)
    me = m.find(user["account_id"])
    if me is None and player_slot is not None:   # если матчи скрыты, account_id может не прийти
        me = next((p for p in m.players if p.slot == player_slot), None)
    if me is not None:
        me.account_id = user["account_id"]
    return m, me


async def make_details(api: Api, user, match_id: int):
    """Подробная статистика: (png, caption), "unparsed" - матч ещё не разобран, None - нет игрока."""
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


def fit_to_medal(mmr: int, rank_tier: int | None) -> int:
    """Самокорректировка: шаг ±25 приблизительный, и счёт бота может уйти от реальной медали.
    Если ушёл - подтягиваем MMR к ближайшей границе диапазона медали из матча."""
    rng = rank_tier_range(rank_tier)
    if not rng:
        return mmr
    lo, hi = rng
    return max(lo, mmr if hi is None else min(mmr, hi))


async def make_report(api: Api, db: DB, user, match_id: int, player_slot: int | None = None,
                      data: dict | None = None, count_mmr: bool = True):
    """Возвращает (png, html) или None, если игрока нет в матче."""
    data = data or await api.match(match_id)
    if not data:
        return None
    m, me = await _load(api, data, user, player_slot)
    if me is None:
        return None

    # MMR: если игрок указал свой MMR - ведём счёт и ранг считаем по нему
    mmr = MmrInfo(ranked=m.is_ranked, old_rank=user["rank_tier"])
    if m.is_ranked:
        won = me.is_radiant == m.radiant_win
        mmr.delta = MMR_STEP if won else -MMR_STEP
    if user["mmr"]:
        mmr.current = user["mmr"] + (mmr.delta if (count_mmr and mmr.delta) else 0)
        if count_mmr:
            mmr.current = fit_to_medal(mmr.current, me.rank_tier)
        new_rank = mmr_to_rank_tier(mmr.current)
        mmr.old_rank = mmr_to_rank_tier(user["mmr"])
    else:
        # медаль из самого матча свежее, чем в профиле OpenDota; из профиля - оценка MMR от OpenDota
        profile = await api.player(user["account_id"]) or {}
        new_rank = me.rank_tier or profile.get("rank_tier")
        mmr.leaderboard = profile.get("leaderboard_rank")
        mmr.approx = True
        if profile.get("computed_mmr"):
            mmr.current, mmr.approx_src = round(profile["computed_mmr"]), "opendota"
        else:
            mmr.current = estimate_mmr(new_rank)
    mmr.new_rank = new_rank
    if count_mmr:
        await db.update_user(user["tg_id"], rank_tier=new_rank,
                             mmr=user["mmr"] if mmr.approx else mmr.current)
    else:
        mmr.old_rank = None   # для ручного запроса изменение ранга не показываем

    avg = await get_averages(api, db, me.hero_id, me.position, new_rank or m.avg_rank_tier, m.duration)
    if PACKS:
        await PACKS.ensure_for_report(me.hero_id, me.items + [me.neutral, me.enhancement], new_rank)
    records = await hero_records(api, user["account_id"], me, data)
    html = build_report(m, me, avg, mmr, api, records)
    png = await build_draft_image(m, me.account_id, api)
    # новая медаль или звезда - для отдельного поздравления (только в отчётах после игры)
    rank_up = (mmr.old_rank, new_rank) if (count_mmr and mmr.old_rank and new_rank and new_rank > mmr.old_rank) else None
    return png, html, short_caption(m, me), m.match_id, {"rank_up": rank_up}


RECORD_MIN_GAMES = 5   # рекорд считаем, если до этого на герое было хотя бы столько игр
RECORD_STATS = [("gold_per_min", "GPM"), ("kills", "убийств"), ("hero_damage", "урона по героям"),
                ("last_hits", "добиваний")]


async def hero_records(api: Api, account_id: int, me, data: dict) -> list[str]:
    """Личные рекорды на герое, которые побил этот матч: «GPM 712 (было 680)»."""
    raw = next((p for p in data.get("players", []) if p.get("player_slot") == me.slot), {})
    try:
        hist = await api.od(f"/players/{account_id}/matches", params=[
            ("hero_id", me.hero_id), *[("project", k) for k, _ in RECORD_STATS]]) or []
    except ApiError as e:
        log.info("records: %s", e)
        return []
    others = [h for h in hist if h.get("match_id") != data.get("match_id")]
    if len(others) < RECORD_MIN_GAMES:
        return []
    out = []
    for key, name in RECORD_STATS:
        mine, best = raw.get(key) or 0, max((h.get(key) or 0) for h in others)
        if mine and best and mine > best:
            out.append(f"{name} <b>{fmt_num(mine)}</b> (было {fmt_num(best)})")
    return out


def webapp_url(user) -> str | None:
    """Ссылка на мини-приложение: Dota ID и MMR (если указан) - в параметрах."""
    if not WEBAPP_URL:
        return None
    url = f"{WEBAPP_URL}/?id={user['account_id']}"
    return url + (f"&mmr={user['mmr']}" if user["mmr"] else "")


async def update_webapp_button(bot: Bot, user):
    """Кнопка «Статистика» у поля ввода (своя ссылка у каждого пользователя)."""
    url = webapp_url(user)
    if not url:
        return
    try:
        await bot.set_chat_menu_button(chat_id=user["chat_id"],
                                       menu_button=MenuButtonWebApp(text="Статистика", web_app=WebAppInfo(url=url)))
    except TelegramBadRequest as e:
        log.warning("menu button for %s: %s", user["tg_id"], e)


async def send_report(bot: Bot, chat_id: int, png: bytes, html: str, caption: str, match_id: int,
                      app_url: str | None = None):
    photo = BufferedInputFile(png, filename=f"match_{match_id}.png")
    kb = match_keyboard(match_id, app_url)

    async def safe(send, text, **kw):
        try:
            await send(chat_id, text, **kw)
        except TelegramBadRequest as e:
            # например, премиум-эмодзи недоступны (у владельца нет Premium) - шлём с обычными
            log.warning("send failed (%s), retrying without custom emoji", e)
            await send(chat_id, strip_custom(text), **kw)

    # одно сообщение: картинка драфта + отчёт подписью (лимит подписи - 1024 символа)
    if _visible_len(html) <= 1024:
        await safe(lambda c, t, **kw: bot.send_photo(c, photo, caption=t, **kw), html, reply_markup=kb)
    else:
        await safe(lambda c, t, **kw: bot.send_photo(c, photo, caption=t, **kw), caption)
        await safe(bot.send_message, html, reply_markup=kb)


def _visible_len(html: str) -> int:
    return len(unescape(re.sub(r"<[^>]+>", "", html)).encode("utf-16-le")) // 2
