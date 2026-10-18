"""Фоновая проверка новых матчей у всех зарегистрированных игроков."""
import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from api import Api, ApiError
from config import PARSE_WAIT_MIN, POLL_ACTIVE, POLL_IDLE
from db import DB
from service import make_report, send_report, update_webapp_button

log = logging.getLogger(__name__)

ACTIVE_WINDOW = 3 * 3600      # если играл последние 3 часа — проверяем чаще
GIVE_UP_AFTER = 3 * 3600      # матч так и не появился в OpenDota
RECHECK_EVERY = 180           # как часто перепроверять матч в очереди
_last_check: dict[tuple[int, int], float] = {}


async def check_user(api: Api, db: DB, user):
    now = int(time.time())
    recent = await api.latest_matches(user["account_id"])
    new = sorted((r for r in recent if r["match_id"] > (user["last_match_id"] or 0)),
                 key=lambda r: r["match_id"])
    last_id, last_played = user["last_match_id"] or 0, user["last_played"] or 0
    if not last_id and recent:   # первый раз видим матчи — старые не присылаем, только запоминаем
        new = []
        last_id = max(r["match_id"] for r in recent)
        last_played = max(r.get("start_time", 0) + r.get("duration", 0) for r in recent)
    for r in new:
        await db.add_pending(r["match_id"], user["tg_id"], r.get("player_slot"))
        await api.request_parse(r["match_id"])
        last_id = max(last_id, r["match_id"])
        last_played = max(last_played, r.get("start_time", 0) + r.get("duration", 0))
    interval = POLL_ACTIVE if now - last_played < ACTIVE_WINDOW else POLL_IDLE
    await db.update_user(user["tg_id"], last_match_id=last_id, last_played=last_played,
                         next_check=now + interval)
    if new:
        log.info("user %s: %d new match(es)", user["tg_id"], len(new))


async def process_pending(bot: Bot, api: Api, db: DB):
    now = int(time.time())
    for p in await db.all_pending():
        user = await db.get_user(p["tg_id"])
        if not user or await db.was_sent(p["match_id"], p["tg_id"]):
            await db.remove_pending(p["match_id"], p["tg_id"])
            continue
        age = now - p["first_seen"]
        key = (p["match_id"], p["tg_id"])
        if now - _last_check.get(key, 0) < RECHECK_EVERY:
            continue
        _last_check[key] = now
        try:
            data = await api.match(p["match_id"])
        except ApiError as e:
            log.warning("match %s: %s", p["match_id"], e)
            continue
        if not data:
            if age > GIVE_UP_AFTER:
                await db.remove_pending(p["match_id"], p["tg_id"])
            continue
        parsed = bool(data.get("version"))
        if not parsed and age < PARSE_WAIT_MIN * 60:
            continue   # ждём разбора, чтобы были нетворс на 10 минуте и позиции
        try:
            res = await make_report(api, db, user, p["match_id"], p["player_slot"], data=data)
            if res:
                png, html, caption, mid = res
                await send_report(bot, user["chat_id"], png, html, caption, mid)
                await db.mark_sent(p["match_id"], p["tg_id"])
                await update_webapp_button(bot, await db.get_user(p["tg_id"]))   # MMR в ссылке обновился
        except TelegramForbiddenError:
            log.info("user %s blocked the bot", user["tg_id"])
            await db.update_user(user["tg_id"], active=0)
        except Exception:  # noqa: BLE001
            log.exception("report for match %s failed", p["match_id"])
            if age < GIVE_UP_AFTER:
                continue   # попробуем ещё раз позже
        await db.remove_pending(p["match_id"], p["tg_id"])
        _last_check.pop(key, None)


async def run_tracker(bot: Bot, api: Api, db: DB):
    log.info("tracker started")
    while True:
        try:
            for user in await db.users_due(int(time.time())):
                try:
                    await check_user(api, db, user)
                except ApiError as e:
                    log.warning("check user %s: %s", user["tg_id"], e)
            await process_pending(bot, api, db)
        except Exception:  # noqa: BLE001
            log.exception("tracker loop error")
        await asyncio.sleep(30)
