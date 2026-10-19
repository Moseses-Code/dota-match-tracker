"""Команды бота: регистрация, профиль, отчёты."""
import logging
import re
import time
from html import escape
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, WebAppInfo

from admin import BTN_ADMIN, is_owner
from analysis import estimate_mmr, mmr_to_rank_tier, rank_name
from api import Api, ApiError
from buttons import BLUE, GREEN, RED, ib, rb
from config import MMR_STEP, OWNER_ID
from db import DB
from emoji import e, hero_e, rank_e, strip_custom
from report import ago, fmt_time, plural
from service import make_details, make_report, send_report, update_webapp_button, webapp_url

log = logging.getLogger(__name__)
router = Router()

STEAM64_BASE = 76561197960265728

# у кнопок меню иконки — премиум-эмодзи, поэтому в тексте эмодзи нет
BTN_LAST = "Последний матч"
BTN_HISTORY = "Мои матчи"
BTN_PROFILE = "Профиль"
BTN_FRIENDS = "Друзья"
BTN_SHARE = "Позвать друга"
BTN_HELP = "Помощь"
BTN_SETTINGS = "Настройки"
REPORT_MODES = [("all", "Все игры"), ("noturbo", "Без турбо"), ("ranked", "Только рейтинг")]
# тексты старого меню: у кого оно ещё открыто, кнопки должны работать
OLD_BTN = {"📊 Последний матч": BTN_LAST, "👤 Профиль": BTN_PROFILE,
           "📨 Позвать друга": BTN_SHARE, "❓ Помощь": BTN_HELP}
SHARE_TEXT = "Бот присылает подробную статистику после каждой игры в Dota 2"
HISTORY_PAGE = 10

MENU_ROWS = [[(BTN_LAST, "last", GREEN)],
             [(BTN_HISTORY, "history", None), (BTN_FRIENDS, "friends", None)],
             [(BTN_PROFILE, "profile", None), (BTN_SHARE, "share", None)],
             [(BTN_SETTINGS, "settings", None), (BTN_HELP, "help", None)]]


def menu(plain: bool = False, owner: bool = False) -> ReplyKeyboardMarkup:
    """Нижнее меню. Собирается при отправке: иконки предметов загружаются после старта бота.
    plain — без иконок и цвета, если Telegram не примет оформление; owner — с кнопкой админки."""
    layout = MENU_ROWS + ([[(BTN_ADMIN, "admin", None)]] if owner else [])
    rows = [[rb(text) if plain else rb(text, key, style) for text, key, style in row] for row in layout]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def answer_menu(msg: Message, text: str):
    """Ответ с нижним меню; если Telegram не принял иконки/цвет — с простым меню."""
    owner = is_owner(msg.chat.id)
    try:
        await msg.answer(text, reply_markup=menu(owner=owner))
    except TelegramBadRequest as err:
        log.warning("menu rejected (%s), sending plain menu", err)
        await msg.answer(text, reply_markup=menu(plain=True, owner=owner))


def btn(name: str):
    """Фильтр кнопки меню — и новый текст, и старый."""
    return F.text.in_({name} | {old for old, new in OLD_BTN.items() if new == name})


def winrate_bar(pct: float, size: int = 10) -> str:
    filled = round(pct / 100 * size)
    return "▰" * filled + "▱" * (size - filled)





class Reg(StatesGroup):
    waiting_id = State()
    waiting_emoji = State()
    waiting_feedback = State()


def parse_account_id(text: str) -> int | None:
    """Принимает Friend ID, Steam64 или ссылку OpenDota/Dotabuff/STRATZ/Steam."""
    text = text.strip()
    m = re.search(r"(?:opendota\.com|dotabuff\.com|stratz\.com)/players/(\d+)", text)
    if not m:
        m = re.search(r"steamcommunity\.com/profiles/(\d{17})", text)
    if not m:
        m = re.fullmatch(r"\d{3,17}", text)
    if not m:
        return None
    n = int(m.group(1) if m.lastindex else m.group(0))
    if n >= STEAM64_BASE:
        n -= STEAM64_BASE
    return n if n > 0 else None


def help_text() -> str:
    return (
        f"{e('stats')} <b>Что я умею</b>\n\n"
        f"После каждой твоей игры присылаю отчёт: картинка драфта, исход, герой и позиция, ранг и MMR, "
        f"{e('kda')} KDA, {e('gpm')} GPM, {e('xpm')} XPM, {e('creeps')} крипы и {e('nw')} нетворс "
        f"в сравнении со средним по твоему рангу и финальный билд.\n\n"
        "<b>Команды</b>\n"
        "/last — последний матч · /match <code>ID</code> — любой матч\n"
        "/history — мои матчи · /friends — с кем играл за месяц\n"
        "/profile — профиль · /mmr <code>2400</code> — указать MMR\n"
        "/invite — позвать друга · /settings — настройки\n"
        "/unlink — отвязать аккаунт\n\n"
        f"{e('warn')} В Dota 2 должна быть включена <b>«Общедоступная история матчей»</b>.\n\n"
        "<i>Бот хранит твой Telegram и Dota ID, ник и историю отправленных отчётов — только для работы бота. "
        "Удалить данные: /unlink.</i>\n\n"
        "Есть вопрос или идея? Жми кнопку ниже 👇"
    )


def welcome_text(user) -> str:
    """Приветствие после привязки аккаунта: что умеет бот и что подробнее — в приложении."""
    name = escape(user["persona"] or "")
    return (
        f"{e('crown')} <b>Готово{', ' + name if name else ''}!</b> Теперь я слежу за твоими играми.\n\n"
        "<b>Что я умею</b>\n"
        f"{e('last')} <b>Отчёт после каждой игры</b> — сам, через несколько минут после матча: драфт, KDA, GPM, "
        "XPM, нетворс, позиция, сравнение со средним по твоему рангу и финальный билд.\n"
        f"{e('more')} <b>«Подробнее»</b> под отчётом — разбор матча: урон, лечение, участие в драках, варды, "
        "итог линии, тайминги предметов.\n"
        f"{e('history')} <b>«Мои матчи»</b> — последние игры, любую можно открыть.\n"
        f"{e('friends')} <b>«Друзья»</b> — с кем играешь чаще всего и как с ними побеждаешь.\n"
        f"{e('rank')} <b>Медаль и MMR</b> — укажи свой: /mmr <code>4200</code>\n"
        f"{e('settings')} <b>«Настройки»</b> — о каких играх присылать отчёты, итоги недели, "
        "предупреждение о серии поражений.\n\n"
        f"{e('winrate')} <b>Ещё больше — в приложении «Статистика»</b> (кнопка слева от поля ввода): графики "
        "винрейта, все матчи с разбором, герои и позиции за любой период, когда ты играешь лучше всего.\n\n"
        "Сыграй матч — отчёт придёт сам 👇"
    )


def settings_view(user) -> tuple[str, InlineKeyboardMarkup]:
    mode = user["report_mode"] or "all"
    text = (f"{e('settings')} <b>Настройки</b>\n\n"
            "<b>Отчёты после игры</b> — о каких матчах присылать.\n"
            "<b>Итоги недели</b> — по воскресеньям в 20:00 (МСК): матчи, винрейт, MMR, герой недели.\n"
            "<b>Серия поражений</b> — после 3 поражений подряд предложу сделать перерыв.")
    on = lambda v: "✅" if v else "⬜"   # noqa: E731
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib(name, style=GREEN if key == mode else None, callback_data=f"set:report:{key}") for key, name in REPORT_MODES],
        [ib(f"{on(user['weekly'])} Итоги недели", callback_data="set:weekly")],
        [ib(f"{on(user['tilt'])} Серия поражений", callback_data="set:tilt")],
    ])
    return text, kb


def setup(api: Api, db: DB):
    @router.message(CommandStart())
    async def start(msg: Message, state: FSMContext):
        await state.clear()
        log.info("/start from tg_id=%s (@%s)", msg.from_user.id, msg.from_user.username)
        user = await db.get_user(msg.from_user.id)
        if user:
            await remember_username(user, msg)
            await update_webapp_button(msg.bot, user)
            await answer_menu(msg, f"{e('wave')} С возвращением, <b>{escape(user['persona'] or '')}</b>!\n"
                                   "Я слежу за твоими играми и пришлю отчёт после каждой.")
            return
        await state.set_state(Reg.waiting_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            ib("Где найти Dota ID?", "wards", BLUE, callback_data="howid")]])
        await msg.answer(
            f"{e('crown')} <b>Dota 2 Match Tracker</b>\n\n"
            "После каждой игры пришлю отчёт с драфтом и статистикой — "
            "в сравнении со средним по твоему рангу.\n\n"
            f"{e('kda')} KDA   {e('gpm')} GPM   {e('xpm')} XPM   {e('nw')} NW\n\n"
            f"{e('matches')} <b>Пришли свой Dota ID</b> или ссылку на профиль, чтобы начать.",
            reply_markup=kb,
        )

    @router.callback_query(F.data == "howid")
    async def how_id(cb: CallbackQuery):
        await cb.message.answer(
            "<b>Где взять Dota ID</b>\n\n"
            "1. Dota 2 → профиль → число под ником (<b>Friend ID</b>)\n"
            "2. или ссылка на профиль <b>OpenDota</b> / <b>STRATZ</b> / <b>Dotabuff</b>\n"
            "3. или ссылка <code>steamcommunity.com/profiles/7656…</code>\n\n"
            f"{e('warn')} Включи в настройках Dota 2 <b>«Общедоступная история матчей»</b>.")
        await cb.answer()

    @router.message(Reg.waiting_id, F.text, ~F.text.startswith("/"))
    async def got_id(msg: Message, state: FSMContext):
        account_id = parse_account_id(msg.text)
        if not account_id:
            await msg.answer(f"{e('warn')} Не понял. Пришли число (Friend ID) или ссылку на профиль.")
            return
        wait = await msg.answer("🔎 Ищу профиль…")
        try:
            player = await api.player(account_id)
            recent = await api.latest_matches(account_id)
        except ApiError:
            await wait.edit_text(f"{e('warn')} OpenDota сейчас не отвечает, попробуй через минуту.")
            return
        profile = (player or {}).get("profile")
        if not profile:
            await wait.edit_text(f"{e('warn')} Профиль <code>{account_id}</code> не найден. Проверь ID.")
            return
        await state.update_data(account_id=account_id, persona=profile.get("personaname"),
                                rank_tier=player.get("rank_tier"),
                                last=recent[0]["match_id"] if recent else 0,
                                last_played=(recent[0]["start_time"] + recent[0]["duration"]) if recent else 0)
        note = "" if recent else (f"\n\n{e('warn')} Матчей не видно — включи в Dota 2 "
                                  "<b>«Общедоступная история матчей»</b>.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            ib("Да, это я", "ok", GREEN, callback_data="reg:yes"),
            ib("Нет", style=RED, callback_data="reg:no"),
        ]])
        await wait.delete()
        caption = (f"Это ты?\n\n<b>{escape(profile.get('personaname') or '?')}</b>\n"
                   f"{e('rank')} {rank_name(player.get('rank_tier'), player.get('leaderboard_rank'))}\n"
                   f"ID: <code>{account_id}</code>{note}")
        if profile.get("avatarfull"):
            await msg.answer_photo(profile["avatarfull"], caption=caption, reply_markup=kb)
        else:
            await msg.answer(caption, reply_markup=kb)

    @router.callback_query(F.data.startswith("reg:"))
    async def reg_confirm(cb: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await cb.message.edit_reply_markup(reply_markup=None)
        if cb.data == "reg:no" or "account_id" not in data:
            await state.set_state(Reg.waiting_id)
            await cb.message.answer("Хорошо, пришли другой ID или ссылку.")
            return await cb.answer()
        await db.upsert_user(cb.from_user.id, cb.message.chat.id, data["account_id"], data["persona"],
                             data["last"], data["last_played"], data["rank_tier"])
        await db.set_username(cb.from_user.id, cb.from_user.username)
        await update_webapp_button(cb.bot, await db.get_user(cb.from_user.id))
        await state.clear()
        user = await db.get_user(cb.from_user.id)
        await answer_menu(cb.message, welcome_text(user))
        rows = []
        if url := webapp_url(user):
            rows.append([ib("Открыть статистику", "more", GREEN, web_app=WebAppInfo(url=url))])
        if data["last"]:
            rows.append([ib("Показать последний матч", "last", BLUE, callback_data="last")])
        if rows:
            await cb.message.answer("С чего начнём? 👇", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await cb.answer()

    async def need_user(msg: Message):
        user = await db.get_user(msg.from_user.id)
        if not user:
            await msg.answer("Сначала привяжи аккаунт: /start")
        else:
            await remember_username(user, msg)
            await refresh_menu(msg)
        return user

    async def remember_username(user, msg: Message):
        """@ник для админки; Telegram позволяет его менять, поэтому обновляем при обращении."""
        if user["username"] != msg.from_user.username:
            await db.set_username(user["tg_id"], msg.from_user.username)

    async def refresh_menu(msg: Message):
        """Нажата кнопка старого меню — присылаем новое (Telegram держит клавиатуру, пока не придёт другая)."""
        if msg.text in OLD_BTN:
            await answer_menu(msg, f"{e('ok')} Меню обновлено: добавились «Мои матчи» и «Друзья» 👇")

    async def report_for(bot: Bot, chat_id: int, user, match_id: int, slot=None):
        try:
            res = await make_report(api, db, user, match_id, slot, count_mmr=False)
        except ApiError:
            await bot.send_message(chat_id, f"{e('warn')} OpenDota не отвечает, попробуй позже.")
            return
        if not res:
            await bot.send_message(chat_id, f"{e('warn')} Не нашёл тебя в матче <code>{match_id}</code>.")
            return
        png, html, caption, mid, _ = res
        await send_report(bot, chat_id, png, html, caption, mid, webapp_url(user))

    async def send_last(bot: Bot, chat_id: int, user):
        await bot.send_chat_action(chat_id, "upload_photo")
        recent = await api.latest_matches(user["account_id"])
        if not recent:
            await bot.send_message(chat_id, f"{e('warn')} Не вижу твоих матчей. Проверь, что история матчей открыта.")
            return
        await report_for(bot, chat_id, user, recent[0]["match_id"], recent[0].get("player_slot"))

    @router.callback_query(F.data == "last")
    async def last_cb(cb: CallbackQuery, bot: Bot):
        await cb.answer()
        user = await db.get_user(cb.from_user.id)
        if user:
            await send_last(bot, cb.message.chat.id, user)

    @router.callback_query(F.data.startswith("more:"))
    async def more_cb(cb: CallbackQuery, bot: Bot):
        await cb.answer()
        user = await db.get_user(cb.from_user.id)
        if not user:
            return
        match_id = int(cb.data.split(":")[1])
        chat_id = cb.message.chat.id
        await bot.send_chat_action(chat_id, "upload_photo")
        try:
            res = await make_details(api, user, match_id)
        except ApiError:
            await bot.send_message(chat_id, f"{e('warn')} OpenDota не отвечает, попробуй позже.")
            return
        if res == "unparsed":
            await bot.send_message(chat_id, f"{e('warn')} Подробная статистика появится после разбора матча — "
                                            "попробуй через несколько минут.")
        elif not res:
            await bot.send_message(chat_id, f"{e('warn')} Не нашёл тебя в матче <code>{match_id}</code>.")
        else:
            png, caption = res
            photo = BufferedInputFile(png, filename=f"details_{match_id}.png")
            try:
                await bot.send_photo(chat_id, photo, caption=caption)
            except TelegramBadRequest:   # премиум-эмодзи не приняты — шлём с обычными
                await bot.send_photo(chat_id, photo, caption=strip_custom(caption))

    @router.message(Command("last"))
    @router.message(btn(BTN_LAST))
    async def last_cmd(msg: Message, bot: Bot):
        if user := await need_user(msg):
            await send_last(bot, msg.chat.id, user)

    async def history_page(user, offset: int):
        """Список матчей текстом (WIN/LOSS — премиум-эмодзи, в кнопку их не вставить)
        и под ним цветные кнопки с номерами матчей."""
        acc = user["account_id"]
        rows = await api.match_history(acc, HISTORY_PAGE + 1, offset)
        has_next = len(rows) > HISTORY_PAGE
        rows = rows[:HISTORY_PAGE]
        items = []   # (match_id, строка, победа: True/False/None)
        if offset == 0:   # свежие матчи, которые OpenDota ещё не показывает в истории
            top = rows[0]["match_id"] if rows else 0
            try:
                fresh = [r for r in await api.latest_matches(acc) if r["match_id"] > top]
            except ApiError:
                fresh = []
            for r in fresh:
                end = (r.get("start_time") or 0) + (r.get("duration") or 0)
                items.append((r["match_id"], f"{e('fire')} Новый матч · {fmt_time(r.get('duration') or 0)}"
                                             f" · {ago(end)}", None))
        for r in rows:
            hid = r.get("hero_id")
            hero = escape(api.heroes.get(hid, {}).get("localized_name", f"#{hid}"))
            won = None if r.get("radiant_win") is None else (r.get("player_slot", 0) < 128) == r["radiant_win"]
            result = e("same") if won is None else e("win" if won else "lose")
            end = (r.get("start_time") or 0) + (r.get("duration") or 0)
            items.append((r["match_id"], f"{result} {hero_e(hid)} {hero} · "
                                         f"{r.get('kills', 0)}/{r.get('deaths', 0)}/{r.get('assists', 0)}"
                                         f" · {fmt_time(r.get('duration') or 0)} · {ago(end)}", won))
        if not items:
            return f"{e('warn')} Не вижу твоих матчей. Проверь, что история матчей открыта.", None

        lines = [f"{e('matches')} <b>Мои матчи</b> · {offset + 1}–{offset + len(rows)}", ""]
        lines += [f"<b>{i}.</b> {text}" for i, (_, text, _) in enumerate(items, 1)]
        lines += ["", "<i>Нажми на номер, чтобы посмотреть отчёт.</i>"]
        numbers = [ib(str(i), style=None if won is None else (GREEN if won else RED), callback_data=f"m:{mid}")
                   for i, (mid, _, won) in enumerate(items, 1)]
        buttons = [numbers[i:i + 5] for i in range(0, len(numbers), 5)]
        nav = []
        if offset > 0:
            nav.append(ib("◀️ Новее", callback_data=f"hist:{max(0, offset - HISTORY_PAGE)}"))
        if has_next:
            nav.append(ib("Старее ▶️", callback_data=f"hist:{offset + HISTORY_PAGE}"))
        if nav:
            buttons.append(nav)
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    @router.message(Command("history"))
    @router.message(btn(BTN_HISTORY))
    async def history_cmd(msg: Message):
        user = await need_user(msg)
        if not user:
            return
        try:
            text, markup = await history_page(user, 0)
        except ApiError:
            await msg.answer(f"{e('warn')} OpenDota не отвечает, попробуй позже.")
            return
        try:
            await msg.answer(text, reply_markup=markup)
        except TelegramBadRequest:   # премиум-эмодзи не приняты — шлём с обычными
            await msg.answer(strip_custom(text), reply_markup=markup)

    @router.callback_query(F.data.startswith("hist:"))
    async def history_cb(cb: CallbackQuery):
        user = await db.get_user(cb.from_user.id)
        if not user:
            return await cb.answer()
        try:
            text, markup = await history_page(user, int(cb.data.split(":")[1]))
        except ApiError:
            return await cb.answer("OpenDota не отвечает, попробуй позже.", show_alert=True)
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            try:
                await cb.message.edit_text(strip_custom(text), reply_markup=markup)
            except TelegramBadRequest:
                pass
        await cb.answer()

    @router.callback_query(F.data.startswith("m:"))
    async def history_match_cb(cb: CallbackQuery, bot: Bot):
        await cb.answer()
        user = await db.get_user(cb.from_user.id)
        if user:
            await bot.send_chat_action(cb.message.chat.id, "upload_photo")
            await report_for(bot, cb.message.chat.id, user, int(cb.data.split(":")[1]))

    @router.message(Command("friends"))
    @router.message(btn(BTN_FRIENDS))
    async def friends_cmd(msg: Message):
        user = await need_user(msg)
        if not user:
            return
        try:
            peers = await api.peers(user["account_id"], days=30)
        except ApiError:
            await msg.answer(f"{e('warn')} OpenDota не отвечает, попробуй позже.")
            return
        month_ago = time.time() - 30 * 86400
        friends = sorted((p for p in peers if p.get("with_games") and (p.get("last_played") or 0) >= month_ago),
                         key=lambda p: p["with_games"], reverse=True)[:5]
        if not friends:
            await msg.answer(f"{e('friends')} <b>Друзья за месяц</b>\n\n"
                             "За последние 30 дней не нашёл игр с друзьями.\n"
                             "<i>Учитываются только игроки с открытой историей матчей.</i>")
            return
        blocks = [f"{e('friends')} <b>Друзья за месяц</b>\n<i>с кем чаще всего играл в одной команде</i>"]
        for i, p in enumerate(friends, 1):
            games, wins = p["with_games"], p.get("with_win") or 0
            wr = wins / games * 100
            place = e("crown") if i == 1 else f"<b>{i}.</b>"
            name = escape(p.get("personaname") or str(p.get("account_id")))
            blocks.append(
                f"<blockquote>{place} <b>{name}</b>\n"
                f"{e('games')} {games} {plural(games, 'игра', 'игры', 'игр')} вместе  ·  "
                f"{e('victory')} {wins} {plural(wins, 'победа', 'победы', 'побед')}\n"
                f"{e('winrate')} Винрейт <b>{wr:.0f}%</b>  {winrate_bar(wr)}\n"
                f"{e('time')} <i>играли {ago(p.get('last_played'))}</i></blockquote>")
        blocks.append("<i>У друзей должна быть открыта история матчей.</i>")
        text = "\n".join(blocks)
        try:
            await msg.answer(text)
        except TelegramBadRequest:   # премиум-эмодзи не приняты — шлём с обычными
            await msg.answer(strip_custom(text))

    @router.message(Command("settings"))
    @router.message(btn(BTN_SETTINGS))
    async def settings_cmd(msg: Message):
        if user := await need_user(msg):
            text, kb = settings_view(user)
            await msg.answer(text, reply_markup=kb)

    @router.callback_query(F.data.startswith("set:"))
    async def settings_cb(cb: CallbackQuery):
        user = await db.get_user(cb.from_user.id)
        if not user:
            return await cb.answer()
        parts = cb.data.split(":")
        if parts[1] == "report" and parts[2] in dict(REPORT_MODES):
            await db.update_user(user["tg_id"], report_mode=parts[2])
        elif parts[1] in ("weekly", "tilt"):
            await db.update_user(user["tg_id"], **{parts[1]: 0 if user[parts[1]] else 1})
        text, kb = settings_view(await db.get_user(cb.from_user.id))
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await cb.answer("Сохранено")

    @router.message(Command("match"))
    async def match_cmd(msg: Message, command: CommandObject, bot: Bot):
        user = await need_user(msg)
        if not user:
            return
        if not command.args or not command.args.strip().isdigit():
            await msg.answer("Использование: /match <code>8123456789</code>")
            return
        await bot.send_chat_action(msg.chat.id, "upload_photo")
        await report_for(bot, msg.chat.id, user, int(command.args.strip()))

    @router.message(Command("profile"))
    @router.message(btn(BTN_PROFILE))
    async def profile_cmd(msg: Message, bot: Bot):
        user = await need_user(msg)
        if not user:
            return
        await bot.send_chat_action(msg.chat.id, "upload_photo")
        acc = user["account_id"]
        try:
            st = await api.profile_stats(acc)
        except ApiError:
            st = {}
        name = st.get("name") or user["persona"] or "?"
        tier = mmr_to_rank_tier(user["mmr"]) if user["mmr"] else user["rank_tier"]
        # без указанного MMR — оценка OpenDota, а если её нет — середина медали
        computed = None
        if not user["mmr"]:
            try:
                computed = (await api.player(acc) or {}).get("computed_mmr")
            except ApiError:
                pass
        approx = round(computed) if computed else estimate_mmr(tier)
        src = "оценка OpenDota" if computed else "примерно, по медали"
        lines = [f"{e('hero')} <b>{escape(name)}</b>",
                 f"{rank_e(tier)} <b>{rank_name(tier)}</b>"
                 + (f" · {user['mmr']} MMR" if user["mmr"] else
                    (f" · ≈{approx} MMR\n<i>MMR — {src}. Укажи точный: /mmr 4200</i>" if approx
                     else " · <i>MMR не указан (/mmr)</i>"))]
        if st.get("matches"):
            wr = st["wins"] / st["matches"] * 100
            lines.append(f"{e('matches')} Матчей: <b>{st['matches']:,}</b>".replace(",", " ")
                         + f" · винрейт <b>{wr:.1f}%</b>")
        if st.get("heroes"):
            lines += ["", f"{e('fav')} <b>Любимые герои</b>"]
            for i, (hid, games, wins) in enumerate(st["heroes"]):
                hname = escape(api.heroes.get(hid, {}).get("localized_name", f"#{hid}"))
                lines.append(f"{hero_e(hid)} {'<b>' + hname + '</b>' if i == 0 else hname}"
                             f" — {games} игр · {wins / max(games, 1) * 100:.0f}%")
        lines += ["", f"ID: <code>{acc}</code>"]
        text = "\n".join(lines)
        rows = [[
            ib("OpenDota", url=f"https://www.opendota.com/players/{acc}"),
            ib("STRATZ", url=f"https://stratz.com/players/{acc}"),
            ib("Dotabuff", url=f"https://www.dotabuff.com/players/{acc}"),
        ]]
        if url := webapp_url(user):
            rows.insert(0, [ib("Открыть статистику", "more", GREEN, web_app=WebAppInfo(url=url))])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        if st.get("avatar"):
            try:
                await msg.answer_photo(st["avatar"], caption=text, reply_markup=kb)
                return
            except TelegramBadRequest:
                pass
        await msg.answer(text, reply_markup=kb)

    @router.message(Command("mmr"))
    async def mmr_cmd(msg: Message, command: CommandObject):
        user = await need_user(msg)
        if not user:
            return
        arg = (command.args or "").strip()
        if not arg.isdigit() or not 0 < int(arg) < 20000:
            await msg.answer("Использование: /mmr <code>4200</code>")
            return
        tier = mmr_to_rank_tier(int(arg))
        await db.update_user(user["tg_id"], mmr=int(arg), rank_tier=tier)
        await update_webapp_button(msg.bot, await db.get_user(user["tg_id"]))
        await msg.answer(f"{e('ok')} Запомнил: <b>{arg}</b> MMR — это {rank_e(tier)} <b>{rank_name(tier)}</b>.\n"
                         f"Дальше буду считать ±{MMR_STEP} за рейтинговую игру "
                         "(это приблизительно — Valve не отдаёт точное изменение). "
                         "Если разойдётся — просто укажи заново.")

    @router.message(Command("unlink"))
    async def unlink_cmd(msg: Message):
        if not await need_user(msg):
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            ib("Да, отвязать", style=RED, callback_data="unlink:yes"),
            ib("Отмена", callback_data="unlink:no"),
        ]])
        await msg.answer("Отвязать аккаунт и перестать присылать отчёты?", reply_markup=kb)

    @router.callback_query(F.data.startswith("unlink:"))
    async def unlink_cb(cb: CallbackQuery):
        await cb.message.edit_reply_markup(reply_markup=None)
        if cb.data == "unlink:yes":
            await db.delete_user(cb.from_user.id)
            await cb.message.answer("Аккаунт отвязан. Чтобы привязать снова — /start")
        await cb.answer()

    @router.message(btn(BTN_SHARE))
    @router.message(Command("invite"))
    async def share_cmd(msg: Message, bot: Bot):
        await refresh_menu(msg)
        me = await bot.get_me()
        link = f"https://t.me/{me.username}"
        markup = InlineKeyboardMarkup(inline_keyboard=[[ib(
            "Отправить другу", "share", BLUE,
            url=f"https://t.me/share/url?url={quote(link)}&text={quote(SHARE_TEXT)}")]])
        await msg.answer(f"Ссылка на бота: {link}\nДруг нажимает /start и привязывает свой аккаунт.",
                         reply_markup=markup)

    @router.message(Command("help"))
    @router.message(btn(BTN_HELP))
    async def help_cmd(msg: Message):
        await refresh_menu(msg)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            ib("Написать владельцу бота", "help", BLUE, callback_data="fb")]])
        await msg.answer(help_text(), reply_markup=markup)

    @router.callback_query(F.data == "fb")
    async def fb_start(cb: CallbackQuery, state: FSMContext):
        await state.set_state(Reg.waiting_feedback)
        markup = InlineKeyboardMarkup(inline_keyboard=[[ib("Отмена", callback_data="fb:cancel")]])
        await cb.message.answer("✍️ Напиши сообщение — вопрос, идею или ошибку. Я передам его владельцу бота, "
                                "он сможет ответить тебе прямо здесь.", reply_markup=markup)
        await cb.answer()

    @router.callback_query(F.data == "fb:cancel")
    async def fb_cancel(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_text("Отменено.")
        await cb.answer()

    @router.message(Reg.waiting_feedback, ~F.text.startswith("/"))
    async def fb_got(msg: Message, state: FSMContext, bot: Bot):
        await state.clear()
        if not OWNER_ID:
            await msg.answer(f"{e('warn')} Обратная связь пока не настроена.")
            return
        u = msg.from_user
        who = f"@{u.username}" if u.username else "без юзернейма"
        user = await db.get_user(u.id)
        dota = f" · Dota ID <code>{user['account_id']}</code>" if user else ""
        head = (f"✉️ <b>Сообщение от пользователя</b>\n"
                f"{escape(u.full_name)} ({who}) · <a href=\"tg://user?id={u.id}\">профиль</a> · "
                f"id <code>{u.id}</code>{dota}\n<i>Ответь на это сообщение (reply), и я перешлю ответ.</i>")
        sent = await bot.send_message(OWNER_ID, head)
        copy = await bot.copy_message(OWNER_ID, msg.chat.id, msg.message_id, reply_to_message_id=sent.message_id)
        await db.add_feedback(sent.message_id, msg.chat.id)
        await db.add_feedback(copy.message_id, msg.chat.id)
        await msg.answer(f"{e('ok')} Отправлено! Владелец бота ответит здесь.")

    @router.message(F.chat.id == OWNER_ID, F.reply_to_message)
    async def owner_reply(msg: Message, bot: Bot):
        chat = await db.feedback_chat(msg.reply_to_message.message_id)
        if not chat:
            return await fallback(msg)
        await bot.send_message(chat, "💬 <b>Ответ от владельца бота:</b>")
        await bot.copy_message(chat, msg.chat.id, msg.message_id)
        await msg.answer(f"{e('ok')} Ответ доставлен.")

    @router.message(Command("emoji"))
    async def emoji_cmd(msg: Message, state: FSMContext):
        await state.set_state(Reg.waiting_emoji)
        await msg.answer("Отправь сообщение с премиум-эмодзи — я пришлю их ID для файла emoji.json.")

    @router.message(Reg.waiting_emoji)
    async def emoji_got(msg: Message, state: FSMContext):
        await state.clear()
        ents = [x for x in (msg.entities or []) if x.type == "custom_emoji"] if msg.text else []
        if not ents:
            await msg.answer("В сообщении нет премиум-эмодзи.")
            return
        lines = []
        for ent in ents:
            char = msg.text.encode("utf-16-le")[ent.offset * 2:(ent.offset + ent.length) * 2].decode("utf-16-le")
            lines.append(f'<tg-emoji emoji-id="{ent.custom_emoji_id}">{char}</tg-emoji> '
                         f"<code>{ent.custom_emoji_id}</code>")
        await msg.answer("\n".join(lines))

    @router.message(F.text)
    async def fallback(msg: Message):
        if await db.get_user(msg.from_user.id):
            await answer_menu(msg, "Не знаю такой команды. /help — список команд.")
        else:
            await msg.answer("Нажми /start, чтобы привязать аккаунт.")

    return router
