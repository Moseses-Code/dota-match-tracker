"""Админка владельца бота (OWNER_ID): сводка, пользователи, выгрузка CSV, рассылка.

Кнопка «Админ» есть только в меню владельца; остальным команды админки не отвечают.
"""
import asyncio
import csv
import io
import logging
import time
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from analysis import rank_name
from buttons import BLUE, GREEN, RED, ib
from config import OWNER_ID
from db import DB
from emoji import e, rank_e
from report import ago

log = logging.getLogger(__name__)

BTN_ADMIN = "Админ"
USERS_PAGE = 10
BROADCAST_DELAY = 0.05   # ~20 сообщений в секунду - ниже лимита Telegram (30/с)


class Admin(StatesGroup):
    waiting_broadcast = State()


def is_owner(chat_id: int | None) -> bool:
    return bool(OWNER_ID) and chat_id == OWNER_ID


def fmt_date(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "-"


def setup(db: DB) -> Router:
    router = Router()
    # все хендлеры роутера - только для владельца
    router.message.filter(F.chat.id == OWNER_ID)
    router.callback_query.filter(F.message.chat.id == OWNER_ID)

    async def panel() -> tuple[str, InlineKeyboardMarkup]:
        s = await db.admin_summary(int(time.time()))
        text = (
            f"{e('admin')} <b>Админка</b>\n"
            "<blockquote>"
            f"<b>Пользователи</b>\n"
            f"Всего: <b>{s['total']}</b>  ·  активных: <b>{s['active']}</b>  ·  отписались: <b>{s['inactive']}</b>\n"
            f"Новых: за день <b>{s['new_day']}</b>  ·  за неделю <b>{s['new_week']}</b>\n"
            f"Играли за неделю: <b>{s['played_week']}</b>"
            "</blockquote>\n<blockquote>"
            f"<b>Отчёты</b>\n"
            f"За день <b>{s['reports_day']}</b>  ·  за неделю <b>{s['reports_week']}</b>  ·  "
            f"всего <b>{s['reports_total']}</b>\n"
            f"Ждут разбора: <b>{s['pending']}</b>"
            "</blockquote>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ib("Пользователи", "friends", BLUE, callback_data="adm:users:0"),
             ib("Выгрузка CSV", "games", callback_data="adm:csv")],
            [ib("Рассылка", "share", GREEN, callback_data="adm:bc")],
            [ib("Обновить", callback_data="adm:panel")],
        ])
        return text, kb

    @router.message(Command("admin"))
    @router.message(F.text == BTN_ADMIN)
    async def admin_cmd(msg: Message, state: FSMContext):
        await state.clear()
        text, kb = await panel()
        await msg.answer(text, reply_markup=kb)

    @router.callback_query(F.data == "adm:panel")
    async def panel_cb(cb: CallbackQuery):
        text, kb = await panel()
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:   # ничего не изменилось
            pass
        await cb.answer()

    # --- пользователи ---
    @router.callback_query(F.data.startswith("adm:users:"))
    async def users_cb(cb: CallbackQuery):
        offset = int(cb.data.split(":")[2])
        total = (await db.admin_summary(int(time.time())))["total"]
        rows = await db.users_page(USERS_PAGE, offset)
        buttons = []
        for u in rows:
            name = u["persona"] or (f"@{u['username']}" if u["username"] else str(u["tg_id"]))
            played = ago(u["last_played"]) if u["last_played"] else "не играл"
            buttons.append([ib(f"{'' if u['active'] else '⛔ '}{name} · {u['reports']} отч. · {played}",
                               callback_data=f"adm:user:{u['tg_id']}:{offset}")])
        nav = []
        if offset > 0:
            nav.append(ib("◀️", callback_data=f"adm:users:{max(0, offset - USERS_PAGE)}"))
        if offset + USERS_PAGE < total:
            nav.append(ib("▶️", callback_data=f"adm:users:{offset + USERS_PAGE}"))
        if nav:
            buttons.append(nav)
        buttons.append([ib("Назад", callback_data="adm:panel")])
        text = (f"{e('friends')} <b>Пользователи</b> · {offset + 1}–{offset + len(rows)} из {total}\n"
                "<i>Новые сверху; ⛔ - отписался. Нажми, чтобы открыть карточку.</i>")
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await cb.answer()

    @router.callback_query(F.data.startswith("adm:user:"))
    async def user_cb(cb: CallbackQuery):
        _, _, tg_id, back = cb.data.split(":")
        u = await db.user_card(int(tg_id))
        if not u:
            return await cb.answer("Пользователь не найден", show_alert=True)
        tier = u["rank_tier"]
        lines = [
            f"{e('profile')} <b>{escape(u['persona'] or '-')}</b>"
            + (f"  ·  @{escape(u['username'])}" if u["username"] else ""),
            "<blockquote>"
            f"Telegram ID: <code>{u['tg_id']}</code>\n"
            f"Dota ID: <code>{u['account_id']}</code>\n"
            f"{rank_e(tier)} {rank_name(tier)}" + (f"  ·  MMR <b>{u['mmr']}</b>" if u["mmr"] else "") + "\n"
            f"Статус: {'активен' if u['active'] else '⛔ отписался'}"
            "</blockquote>\n<blockquote>"
            f"Зарегистрирован: {fmt_date(u['created_at'])}\n"
            f"Последняя игра: {fmt_date(u['last_played'])}\n"
            f"Отчётов: <b>{u['reports']}</b>  ·  последний: {fmt_date(u['last_report'])}"
            "</blockquote>",
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [ib("OpenDota", url=f"https://www.opendota.com/players/{u['account_id']}"),
             ib("STRATZ", url=f"https://stratz.com/players/{u['account_id']}")],
            [ib("Назад", callback_data=f"adm:users:{back}")],
        ])
        await cb.message.edit_text("\n".join(lines), reply_markup=kb)
        await cb.answer()

    # --- выгрузка ---
    @router.callback_query(F.data == "adm:csv")
    async def csv_cb(cb: CallbackQuery, bot: Bot):
        await cb.answer()
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")   # «;» - Excel в русской локали сразу делит на столбцы
        w.writerow(["telegram_id", "username", "dota_id", "ник", "медаль", "mmr", "активен",
                    "зарегистрирован", "последняя игра", "отчётов", "последний отчёт"])
        for u in await db.all_users():
            w.writerow([u["tg_id"], u["username"] or "", u["account_id"], u["persona"] or "",
                        rank_name(u["rank_tier"]), u["mmr"] or "", "да" if u["active"] else "нет",
                        fmt_date(u["created_at"]), fmt_date(u["last_played"]), u["reports"],
                        fmt_date(u["last_report"])])
        data = out.getvalue().encode("utf-8-sig")   # BOM - чтобы Excel открыл кириллицу
        name = f"users_{datetime.now():%Y-%m-%d}.csv"
        await bot.send_document(cb.message.chat.id, BufferedInputFile(data, filename=name),
                                caption=f"{e('games')} Пользователи бота")

    # --- рассылка ---
    @router.callback_query(F.data == "adm:bc")
    async def bc_start(cb: CallbackQuery, state: FSMContext):
        await state.set_state(Admin.waiting_broadcast)
        await cb.message.answer(
            f"{e('share')} <b>Рассылка</b>\n\n"
            "Пришли сообщение для рассылки - текст, фото с подписью, видео, что угодно. "
            "Оформление и премиум-эмодзи сохранятся. Перед отправкой покажу, как оно выглядит.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ib("Отмена", callback_data="adm:bc:cancel")]]))
        await cb.answer()

    @router.message(Admin.waiting_broadcast)
    async def bc_got(msg: Message, state: FSMContext, bot: Bot):
        await state.update_data(bc_msg=msg.message_id)
        chats = [c for c in await db.active_chats() if c["chat_id"] != OWNER_ID]
        await msg.answer("👇 Так увидят пользователи:")
        await bot.copy_message(msg.chat.id, msg.chat.id, msg.message_id)
        await msg.answer(
            f"Отправить <b>{len(chats)}</b> активным пользователям?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                ib(f"Отправить ({len(chats)})", "share", GREEN, callback_data="adm:bc:go"),
                ib("Отмена", style=RED, callback_data="adm:bc:cancel")]]))

    @router.callback_query(F.data == "adm:bc:cancel")
    async def bc_cancel(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_text("Рассылка отменена.")
        await cb.answer()

    @router.callback_query(F.data == "adm:bc:go")
    async def bc_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
        msg_id = (await state.get_data()).get("bc_msg")
        await state.clear()
        if not msg_id:
            return await cb.answer("Сообщение для рассылки не найдено", show_alert=True)
        await cb.message.edit_text("⏳ Рассылка идёт…")
        await cb.answer()
        asyncio.create_task(broadcast(bot, cb.message.chat.id, msg_id, cb.message))

    async def broadcast(bot: Bot, from_chat: int, msg_id: int, status: Message):
        from handlers import menu   # здесь, а не наверху: handlers сам импортирует admin
        chats = [c for c in await db.active_chats() if c["chat_id"] != OWNER_ID]
        markup = menu()   # заодно у всех обновится нижнее меню (новые кнопки)
        sent = blocked = failed = 0
        for c in chats:
            for _ in range(3):
                try:
                    try:
                        await bot.copy_message(c["chat_id"], from_chat, msg_id, reply_markup=markup)
                    except TelegramBadRequest:   # меню с иконками не принято - без меню
                        await bot.copy_message(c["chat_id"], from_chat, msg_id)
                    sent += 1
                except TelegramRetryAfter as err:
                    await asyncio.sleep(err.retry_after + 1)
                    continue
                except TelegramForbiddenError:   # заблокировал бота
                    blocked += 1
                    await db.update_user(c["tg_id"], active=0)
                except TelegramBadRequest as err:
                    failed += 1
                    log.info("broadcast to %s failed: %s", c["chat_id"], err)
                break
            await asyncio.sleep(BROADCAST_DELAY)
        log.info("broadcast done: sent=%d blocked=%d failed=%d", sent, blocked, failed)
        await status.edit_text(
            f"{e('ok')} <b>Рассылка завершена</b>\n"
            f"Доставлено: <b>{sent}</b>  ·  заблокировали бота: <b>{blocked}</b>  ·  ошибки: <b>{failed}</b>")

    return router
