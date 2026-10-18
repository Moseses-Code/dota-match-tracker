"""Команды бота: регистрация, профиль, отчёты."""
import logging
import re
from html import escape
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from analysis import mmr_to_rank_tier, rank_name
from api import Api, ApiError
from config import MMR_STEP, OWNER_ID
from db import DB
from emoji import e, hero_e, rank_e
from service import make_details, make_report, send_report

log = logging.getLogger(__name__)
router = Router()

STEAM64_BASE = 76561197960265728

BTN_LAST = "📊 Последний матч"
BTN_PROFILE = "👤 Профиль"
BTN_SHARE = "📨 Позвать друга"
BTN_HELP = "❓ Помощь"
SHARE_TEXT = "Бот присылает подробную статистику после каждой игры в Dota 2"

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text=BTN_LAST), KeyboardButton(text=BTN_PROFILE)],
    [KeyboardButton(text=BTN_SHARE), KeyboardButton(text=BTN_HELP)],
], resize_keyboard=True)


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
        "/profile — профиль · /mmr <code>2400</code> — указать MMR\n"
        "/invite — позвать друга · /unlink — отвязать аккаунт\n\n"
        f"{e('warn')} В Dota 2 должна быть включена <b>«Общедоступная история матчей»</b>.\n\n"
        "Есть вопрос или идея? Жми кнопку ниже 👇"
    )


def setup(api: Api, db: DB):
    @router.message(CommandStart())
    async def start(msg: Message, state: FSMContext):
        await state.clear()
        log.info("/start from tg_id=%s (@%s)", msg.from_user.id, msg.from_user.username)
        user = await db.get_user(msg.from_user.id)
        if user:
            await msg.answer(f"{e('wave')} С возвращением, <b>{escape(user['persona'] or '')}</b>!\n"
                             "Я слежу за твоими играми и пришлю отчёт после каждой.", reply_markup=MENU)
            return
        await state.set_state(Reg.waiting_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Где найти Dota ID?", callback_data="howid")]])
        await msg.answer(
            f"{e('win')} <b>Dota 2 Match Tracker</b>\n\n"
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
            InlineKeyboardButton(text="✅ Да, это я", callback_data="reg:yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="reg:no"),
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
        await state.clear()
        await cb.message.answer(
            f"{e('ok')} <b>Готово!</b> Теперь я слежу за твоими играми — отчёт придёт через "
            "несколько минут после окончания матча.\n\n"
            "Совет: укажи текущий MMR командой /mmr <code>4200</code>, и я буду считать изменения.",
            reply_markup=MENU,
        )
        if data["last"]:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📊 Показать последний матч", callback_data="last")]])
            await cb.message.answer("Хочешь посмотреть отчёт о последней игре?", reply_markup=kb)
        await cb.answer()

    async def need_user(msg: Message):
        user = await db.get_user(msg.from_user.id)
        if not user:
            await msg.answer("Сначала привяжи аккаунт: /start")
        return user

    async def report_for(bot: Bot, chat_id: int, user, match_id: int, slot=None):
        try:
            res = await make_report(api, db, user, match_id, slot, count_mmr=False)
        except ApiError:
            await bot.send_message(chat_id, f"{e('warn')} OpenDota не отвечает, попробуй позже.")
            return
        if not res:
            await bot.send_message(chat_id, f"{e('warn')} Не нашёл тебя в матче <code>{match_id}</code>.")
            return
        png, html, caption, mid = res
        await send_report(bot, chat_id, png, html, caption, mid)

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
            await bot.send_photo(chat_id, BufferedInputFile(png, filename=f"details_{match_id}.png"),
                                 caption=caption)

    @router.message(Command("last"))
    @router.message(F.text == BTN_LAST)
    async def last_cmd(msg: Message, bot: Bot):
        if user := await need_user(msg):
            await send_last(bot, msg.chat.id, user)

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
    @router.message(F.text == BTN_PROFILE)
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
        lines = [f"{e('hero')} <b>{escape(name)}</b>",
                 f"{rank_e(tier)} <b>{rank_name(tier)}</b>"
                 + (f" · {user['mmr']} MMR" if user["mmr"] else " · <i>MMR не указан (/mmr)</i>")]
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
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="OpenDota", url=f"https://www.opendota.com/players/{acc}"),
            InlineKeyboardButton(text="STRATZ", url=f"https://stratz.com/players/{acc}"),
            InlineKeyboardButton(text="Dotabuff", url=f"https://www.dotabuff.com/players/{acc}"),
        ]])
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
        await msg.answer(f"{e('ok')} Запомнил: <b>{arg}</b> MMR — это {rank_e(tier)} <b>{rank_name(tier)}</b>.\n"
                         f"Дальше буду считать ±{MMR_STEP} за рейтинговую игру "
                         "(это приблизительно — Valve не отдаёт точное изменение). "
                         "Если разойдётся — просто укажи заново.")

    @router.message(Command("unlink"))
    async def unlink_cmd(msg: Message):
        if not await need_user(msg):
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, отвязать", callback_data="unlink:yes"),
            InlineKeyboardButton(text="Отмена", callback_data="unlink:no"),
        ]])
        await msg.answer("Отвязать аккаунт и перестать присылать отчёты?", reply_markup=kb)

    @router.callback_query(F.data.startswith("unlink:"))
    async def unlink_cb(cb: CallbackQuery):
        await cb.message.edit_reply_markup(reply_markup=None)
        if cb.data == "unlink:yes":
            await db.delete_user(cb.from_user.id)
            await cb.message.answer("Аккаунт отвязан. Чтобы привязать снова — /start")
        await cb.answer()

    @router.message(F.text == BTN_SHARE)
    @router.message(Command("invite"))
    async def share_cmd(msg: Message, bot: Bot):
        me = await bot.get_me()
        link = f"https://t.me/{me.username}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📨 Отправить другу",
            url=f"https://t.me/share/url?url={quote(link)}&text={quote(SHARE_TEXT)}")]])
        await msg.answer(f"Ссылка на бота: {link}\nДруг нажимает /start и привязывает свой аккаунт.",
                         reply_markup=kb)

    @router.message(Command("help"))
    @router.message(F.text == BTN_HELP)
    async def help_cmd(msg: Message):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✉️ Написать владельцу бота", callback_data="fb")]])
        await msg.answer(help_text(), reply_markup=kb)

    @router.callback_query(F.data == "fb")
    async def fb_start(cb: CallbackQuery, state: FSMContext):
        await state.set_state(Reg.waiting_feedback)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="fb:cancel")]])
        await cb.message.answer("✍️ Напиши сообщение — вопрос, идею или ошибку. Я передам его владельцу бота, "
                                "он сможет ответить тебе прямо здесь.", reply_markup=kb)
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
            await msg.answer("Не знаю такой команды. /help — список команд.", reply_markup=MENU)
        else:
            await msg.answer("Нажми /start, чтобы привязать аккаунт.")

    return router
