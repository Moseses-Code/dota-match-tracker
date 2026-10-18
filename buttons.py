"""Кнопки с цветом и иконкой (Bot API 9.4+: style и icon_custom_emoji_id).

Иконки — премиум-эмодзи, работают, если у владельца бота есть Telegram Premium.
Если с ними что-то не так, их можно выключить: BUTTON_ICONS=0 в .env.
"""
from aiogram.types import InlineKeyboardButton, KeyboardButton

from config import BUTTON_ICONS
from emoji import dynamic, icon

GREEN, BLUE, RED = "success", "primary", "danger"


def _icon(key: str | None) -> str | None:
    if not (BUTTON_ICONS and key):
        return None
    return dynamic.get(key) if ":" in key else icon(key)   # "hero:74" — иконка героя из пака бота


def ib(text: str, icon_key: str | None = None, style: str | None = None, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=_icon(icon_key), style=style, **kw)


def rb(text: str, icon_key: str | None = None, style: str | None = None) -> KeyboardButton:
    return KeyboardButton(text=text, icon_custom_emoji_id=_icon(icon_key), style=style)
