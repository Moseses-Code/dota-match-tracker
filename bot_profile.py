"""Описание бота в Telegram: то, что видно в пустом чате до нажатия «Старт», и в профиле бота."""
import logging

from aiogram import Bot

log = logging.getLogger(__name__)

# до 512 символов — показывается в пустом чате
DESCRIPTION = (
    "⚔️ Dota 2 Match Tracker\n\n"
    "После каждой твоей игры — отчёт прямо в Telegram:\n\n"
    "🖼 картинка драфта и исход матча\n"
    "📊 KDA, GPM, XPM, крипы, нетворс\n"
    "📈 сравнение со средним по твоему рангу и герою\n"
    "🏅 ранг и изменение MMR\n"
    "🎒 финальный билд\n\n"
    "Работает для тебя и друзей — каждый привязывает свой аккаунт.\n\n"
    "Жми «Старт» 👇"
)

# до 120 символов — в профиле бота и при пересылке ссылки
SHORT_DESCRIPTION = "📊 Статистика каждой твоей игры в Dota 2 со сравнением со средним по рангу"


async def apply(bot: Bot):
    """Обновляет описания, только если они изменились (у Telegram лимит на частые изменения)."""
    try:
        if (await bot.get_my_description()).description != DESCRIPTION:
            await bot.set_my_description(DESCRIPTION)
        if (await bot.get_my_short_description()).short_description != SHORT_DESCRIPTION:
            await bot.set_my_short_description(SHORT_DESCRIPTION)
    except Exception as e:  # noqa: BLE001
        log.warning("bot description: %s", e)
