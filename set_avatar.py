"""Ставит боту аватарку из avatar.png (Bot API 9.4: setMyProfilePhoto). Запуск: python set_avatar.py"""
import asyncio
import io

from aiogram import Bot
from aiogram.types import BufferedInputFile, InputProfilePhotoStatic
from PIL import Image

from config import BASE_DIR, BOT_TOKEN


async def main():
    img = Image.open(BASE_DIR / "avatar.png").convert("RGB").resize((640, 640), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    bot = Bot(BOT_TOKEN)
    try:
        photo = InputProfilePhotoStatic(photo=BufferedInputFile(buf.getvalue(), "avatar.jpg"))
        print("ok" if await bot.set_my_profile_photo(photo=photo) else "failed")
    finally:
        await bot.session.close()


asyncio.run(main())
