import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, LinkPreviewOptions

import bot_profile
import handlers
import service
from custom_packs import Packs
from api import Api
from config import BOT_TOKEN
from db import DB
from tracker import run_tracker

COMMANDS = [
    BotCommand(command="last", description="Отчёт о последнем матче"),
    BotCommand(command="match", description="Отчёт о матче по ID"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="mmr", description="Указать текущий MMR"),
    BotCommand(command="invite", description="Позвать друга"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="unlink", description="Отвязать аккаунт"),
]


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в файле .env")

    db = DB()
    await db.open()
    api = Api()
    await api.open()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers.setup(api, db))
    await bot.set_my_commands(COMMANDS)
    await bot_profile.apply(bot)

    service.PACKS = Packs(bot, api, db)
    await service.PACKS.load()
    prewarm = asyncio.create_task(service.PACKS.prewarm())
    tracker = asyncio.create_task(run_tracker(bot, api, db))
    try:
        await dp.start_polling(bot)
    finally:
        tracker.cancel()
        prewarm.cancel()
        await api.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
