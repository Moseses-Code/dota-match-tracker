"""Уведомления помимо отчётов: новая медаль, серия поражений, итоги недели."""
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, WebAppInfo

from analysis import rank_name
from api import Api, ApiError
from buttons import GREEN, ib
from config import MMR_STEP, TZ_OFFSET, WEEKLY_HOUR
from db import DB
from emoji import e, hero_e, rank_e, strip_custom
from report import fmt_time, plural
from service import webapp_url

log = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=TZ_OFFSET))


def app_button(user, text="Открыть статистику") -> InlineKeyboardMarkup | None:
    url = webapp_url(user)
    return InlineKeyboardMarkup(inline_keyboard=[[ib(text, "more", GREEN, web_app=WebAppInfo(url=url))]]) if url else None


async def send(bot: Bot, user, text: str, kb=None):
    try:
        await bot.send_message(user["chat_id"], text, reply_markup=kb)
    except TelegramBadRequest:   # премиум-эмодзи не приняты - с обычными
        await bot.send_message(user["chat_id"], strip_custom(text), reply_markup=kb)


async def send_rank_up(bot: Bot, user, old: int, new: int):
    what = "новая медаль" if new // 10 != old // 10 else "новая звезда"
    await send(bot, user, f"{e('new_rank')} <b>Поздравляю - {what}!</b>\n\n"
                          f"{rank_e(old)} {rank_name(old)}  →  {rank_e(new)} <b>{rank_name(new)}</b>\n\n"
                          "Так держать 💪")


async def send_tilt(bot: Bot, user, streak: int):
    await send(bot, user,
               f"{e('tilt')} <b>{streak} {plural(streak, 'поражение', 'поражения', 'поражений')} подряд</b>\n\n"
               "Бывает у всех. Короткий перерыв - 15 минут, вода, размяться - часто помогает лучше, "
               "чем ещё одна игра на эмоциях.\n\n"
               "<i>Это предупреждение можно выключить в /settings.</i>")


async def maybe_weekly(bot: Bot, api: Api, db: DB):
    """Итоги недели: в воскресенье после WEEKLY_HOUR - всем, кто их ещё не получил на этой неделе."""
    now = datetime.now(TZ)
    if now.weekday() != 6 or now.hour < WEEKLY_HOUR:
        return
    since = int(now.replace(hour=WEEKLY_HOUR, minute=0, second=0, microsecond=0).timestamp())   # уже отправлено сегодня?
    for user in await db.weekly_due(since):
        await db.update_user(user["tg_id"], weekly_sent=int(time.time()))   # даже если не играл - не спрашиваем снова
        try:
            text = await weekly_text(api, user)
            if text:
                await send(bot, user, text, app_button(user))
        except TelegramForbiddenError:
            await db.update_user(user["tg_id"], active=0)
        except ApiError as err:
            log.info("weekly for %s: %s", user["tg_id"], err)


async def weekly_text(api: Api, user) -> str | None:
    rows = await api.od(f"/players/{user['account_id']}/matches", params=[
        ("date", 7), *[("project", k) for k in ("hero_id", "kills", "deaths", "assists", "player_slot",
                                                "radiant_win", "lobby_type", "duration")]]) or []
    if not rows:
        return None
    for r in rows:
        r["win"] = (r.get("player_slot", 0) < 128) == r.get("radiant_win")
        r["kda"] = ((r.get("kills") or 0) + (r.get("assists") or 0)) / max(1, r.get("deaths") or 0)
    wins = sum(r["win"] for r in rows)
    ranked = [r for r in rows if r.get("lobby_type") == 7]
    rw = sum(r["win"] for r in ranked)

    def hero(hid):
        return f"{hero_e(hid)} {escape(api.heroes.get(hid, {}).get('localized_name', f'#{hid}'))}"

    def match_line(r):
        return (f"{hero(r['hero_id'])} {r['kills']}/{r['deaths']}/{r['assists']} · "
                f"{'победа' if r['win'] else 'поражение'} · {fmt_time(r.get('duration') or 0)}")

    fav, fav_games = Counter(r["hero_id"] for r in rows).most_common(1)[0]
    fav_wins = sum(r["win"] for r in rows if r["hero_id"] == fav)
    best, worst = max(rows, key=lambda r: r["kda"]), min(rows, key=lambda r: r["kda"])
    lines = [f"{e('weekly')} <b>Итоги недели</b>", "<blockquote>"
             f"{e('games')} {len(rows)} {plural(len(rows), 'матч', 'матча', 'матчей')} · "
             f"{wins} {plural(wins, 'победа', 'победы', 'побед')} · <b>{round(wins / len(rows) * 100)}%</b>"]
    if ranked:
        delta = (rw - (len(ranked) - rw)) * MMR_STEP
        lines.append(f"{e('rank')} Рейтинг: {rw}–{len(ranked) - rw} · MMR <b>{'+' if delta >= 0 else '−'}{abs(delta)}</b>")
    lines[-1] += "</blockquote>"
    lines += [f"{e('fav')} Герой недели: {hero(fav)} - {fav_games} {plural(fav_games, 'игра', 'игры', 'игр')}, "
              f"{round(fav_wins / fav_games * 100)}% побед",
              f"{e('streak')} Лучший матч: {match_line(best)}"]
    if worst is not best:
        lines.append(f"{e('death')} Худший матч: {match_line(worst)}")
    lines.append("\n<i>Подробности - в приложении «Статистика».</i>")
    return "\n".join(lines)
