"""Фоновая проверка новых матчей у всех зарегистрированных игроков."""
import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from api import BACKGROUND, Api, ApiError
from config import PARSE_WAIT_MIN, POLL_ACTIVE, POLL_IDLE
from db import DB
from notify import maybe_weekly, send_rank_up, send_tilt
from service import make_report, send_report, update_webapp_button, webapp_url

log = logging.getLogger(__name__)

ACTIVE_WINDOW = 3 * 3600      # если играл последние 3 часа — проверяем чаще
GIVE_UP_AFTER = 3 * 3600      # матч так и не появился в OpenDota
RECHECK_EVERY = 180           # как часто перепроверять матч в очереди
TILT_AFTER = 3                # предупреждение после стольких поражений подряд
_last_check: dict[tuple[int, int], float] = {}


async def check_user(api: Api, db: DB, user):
    now = int(time.time())
    recent = await api.latest_matches(user["account_id"], known_last=user["last_match_id"] or 0)
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
            if wanted(user, data):
                res = await make_report(api, db, user, p["match_id"], p["player_slot"], data=data)
                if res:
                    png, html, caption, mid, extra = res
                    await send_report(bot, user["chat_id"], png, html, caption, mid, webapp_url(user))
                    if extra["rank_up"]:
                        await send_rank_up(bot, user, *extra["rank_up"])
                    await update_webapp_button(bot, await db.get_user(p["tg_id"]))   # MMR в ссылке обновился
            await db.mark_sent(p["match_id"], p["tg_id"])
            # серия считается после отправки: при повторной попытке поражение не засчитается дважды
            streak = await update_streak(db, user, data, p["player_slot"])
            if streak == TILT_AFTER and user["tilt"]:
                await send_tilt(bot, user, streak)
        except TelegramForbiddenError:
            log.info("user %s blocked the bot", user["tg_id"])
            await db.update_user(user["tg_id"], active=0)
        except Exception:  # noqa: BLE001
            log.exception("report for match %s failed", p["match_id"])
            if age < GIVE_UP_AFTER:
                continue   # попробуем ещё раз позже
        await db.remove_pending(p["match_id"], p["tg_id"])
        _last_check.pop(key, None)


def wanted(user, data: dict) -> bool:
    """Нужен ли отчёт об этой игре по настройкам игрока: все / без турбо / только рейтинг."""
    mode = user["report_mode"] or "all"
    if mode == "ranked":
        return data.get("lobby_type") == 7
    if mode == "noturbo":
        return data.get("game_mode") != 23
    return True


async def update_streak(db: DB, user, data: dict, player_slot: int | None) -> int:
    """Поражений подряд после этой игры (0 — победа или игрока не нашли)."""
    me = next((p for p in data.get("players", []) if p.get("account_id") == user["account_id"]
               or (player_slot is not None and p.get("player_slot") == player_slot)), None)
    if me is None or data.get("radiant_win") is None:
        return 0
    won = (me.get("player_slot", 0) < 128) == data["radiant_win"]
    streak = 0 if won else (user["loss_streak"] or 0) + 1
    await db.update_user(user["tg_id"], loss_streak=streak)
    return streak


async def run_tracker(bot: Bot, api: Api, db: DB):
    log.info("tracker started")
    BACKGROUND.set(True)   # запросы трекера уступают очередь командам пользователей
    while True:
        try:
            for user in await db.users_due(int(time.time())):
                try:
                    await check_user(api, db, user)
                except ApiError as e:
                    log.warning("check user %s: %s", user["tg_id"], e)
            await process_pending(bot, api, db)
            await maybe_weekly(bot, api, db)
        except Exception:  # noqa: BLE001
            log.exception("tracker loop error")
        await asyncio.sleep(30)
