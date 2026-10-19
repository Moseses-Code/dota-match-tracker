"""Эмодзи для сообщений.

Оформление - анимированные эмодзи из официального пака Telegram «Animated Emoji» (RestrictedEmoji).
Иконки героев, предметов и медалей - из собственных паков бота (их создаёт custom_packs.py).
Премиум-эмодзи работают, только если у владельца бота есть Telegram Premium (Bot API 9.4+).
Любой ключ можно переопределить в emoji.json (ID узнаётся командой /emoji).
"""
import json
import re

from config import EMOJI_FILE

# ключ: (обычный эмодзи, custom_emoji_id из пака RestrictedEmoji)
DEFAULTS = {
    "win": ("🏆", "5409008750893734809"),
    "lose": ("💀", "5370971163310693562"),
    "time": ("⏳", "5451732530048802485"),
    "hero": ("🎮", "5467583879948803288"),
    "pos": ("🎯", "5350460637182993292"),
    "rank": ("🏅", "5334644364280866007"),
    "mmr_up": ("📈", "5373001317042101552"),
    "mmr_down": ("📉", "5361748661640372834"),
    "kda": ("⚔️", "5408935401442267103"),
    "gpm": ("💰", "5375296873982604963"),
    "xpm": ("📚", "5373098009640836781"),
    "creeps": ("🗡", "5393607290228067750"),
    "nw": ("💎", "5471952986970267163"),
    "nw10": ("⌛", "5451646226975955576"),
    "items": ("🔮", "5361837567463399422"),
    "neutral": ("🌿", "5449850741667668411"),
    "up": ("📈", "5373001317042101552"),
    "down": ("📉", "5361748661640372834"),
    "same": ("➖", "5229113891081956317"),
    "stats": ("📊", "5431577498364158238"),
    "fire": ("🔥", "5420315771991497307"),
    "crown": ("👑", "5467406098367521267"),
    "link": ("🔗", "5375129357373165375"),
    "wave": ("👋", "5472055112702629499"),
    "ok": ("✅", "5427009714745517609"),
    "warn": ("❗", "5467928559664242360"),
    "radiant": ("☀️", "5469947168523558652"),   # Силы Света
    "dire": ("🌑", "5188497854242495901"),      # Силы Тьмы
    "new_rank": ("🎉", "5436040291507247633"),
    "fav": ("❤️", "5449505950283078474"),
    "matches": ("👁", "5424892643760937442"),
    "party": ("🥳", "5370870691140737817"),
    "sad": ("😭", "5370646412243510708"),
    # запасные эмодзи для тематических значков (THEMED)
    "damage": ("⚔️", "5408935401442267103"),
    "heal": ("💚", "5449505950283078474"),
    "fights": ("🎯", "5350460637182993292"),
    "wards": ("👁", "5424892643760937442"),
    "lane": ("🌿", "5449850741667668411"),
    "timing": ("⏳", "5451732530048802485"),
    "streak": ("🔥", "5420315771991497307"),
    "death": ("🛡", "5409008750893734809"),
    "friends": ("🥳", "5370870691140737817"),
    "games": ("🎮", "5467583879948803288"),
    "victory": ("🏆", "5409008750893734809"),
    "winrate": ("🏆", "5409008750893734809"),
    "last": ("📊", "5431577498364158238"),
    "history": ("⏳", "5451732530048802485"),
    "profile": ("🎮", "5467583879948803288"),
    "share": ("🔗", "5375129357373165375"),
    "help": ("❗", "5467928559664242360"),
    "more": ("📊", "5431577498364158238"),
    "admin": ("👑", "5467406098367521267"),
    "settings": ("⚙️", "5431577498364158238"),
    "weekly": ("📅", "5451732530048802485"),
    "tilt": ("😮‍💨", "5370646412243510708"),
}

# тематические значки: ключ -> иконка предмета Dota 2 из пака бота (custom_packs, emoji_map.json);
# если иконки нет - обычный эмодзи из DEFAULTS
THEMED = {
    "damage": "item:greater_crit",      # урон - Daedalus
    "heal": "item:guardian_greaves",    # лечение
    "fights": "item:blink",             # драки
    "wards": "item:ward_observer",
    "lane": "item:tango",               # линия
    "timing": "item:black_king_bar",    # тайминги предметов
    "streak": "item:bloodstone",        # серии убийств
    "death": "item:aegis",              # смерти и выкупы - Aegis
    "friends": "item:manta",            # друзья - Manta Style (иллюзии-копии)
    "games": "item:tpscroll",
    "victory": "item:aegis",
    "winrate": "item:aegis",
    # кнопки
    "last": "item:tpscroll",
    "history": "item:travel_boots",
    "profile": "item:aghanims_shard",
    "share": "item:smoke_of_deceit",
    "help": "item:clarity",
    "more": "item:gem",
    "admin": "item:ultimate_scepter",
    "settings": "item:bottle",
    "weekly": "item:tome_of_knowledge",
    "tilt": "item:refresher",
}

_override: dict[str, list[str]] = {}
# динамические иконки (hero:74, item:blink, rank:65) -> custom_emoji_id; заполняет custom_packs
dynamic: dict[str, str] = {}


def load():
    global _override
    if EMOJI_FILE.exists():
        data = json.loads(EMOJI_FILE.read_text("utf-8"))
        # значение - один ID или список ID (слово из нескольких эмодзи, например WIN)
        _override = {k: [str(x) for x in (v if isinstance(v, list) else [v])]
                     for k, v in data.items() if v and not k.startswith("_")}


def tg(cid: str | None, char: str) -> str:
    return f'<tg-emoji emoji-id="{cid}">{char}</tg-emoji>' if cid else char


def _ids(key: str) -> list[str]:
    """ID эмодзи значка: emoji.json → иконка оформления → тематическая иконка предмета → стандартный."""
    if key in _override:
        return _override[key]
    cid = dynamic.get(f"ui:{key}") or dynamic.get(THEMED.get(key, "")) or DEFAULTS.get(key, ("", None))[1]
    return [cid] if cid else []


def e(key: str) -> str:
    char = DEFAULTS.get(key, ("•", None))[0]
    return "".join(tg(x, char) for x in _ids(key)) or char


def icon(key: str) -> str | None:
    """custom_emoji_id для иконки кнопки (первый, если значок из нескольких эмодзи)."""
    ids = _ids(key)
    return ids[0] if ids else None


def hero_e(hero_id: int) -> str:
    return tg(dynamic.get(f"hero:{hero_id}"), "🦸") if f"hero:{hero_id}" in dynamic else e("hero")


def item_e(item_name: str) -> str | None:
    cid = dynamic.get(f"item:{item_name}")
    return tg(cid, "🗡") if cid else None


def rank_e(rank_tier: int | None) -> str:
    cid = dynamic.get(f"rank:{rank_tier}") if rank_tier else None
    return tg(cid, "🏅") if cid else e("rank")


def strip_custom(html: str) -> str:
    """Убирает премиум-эмодзи, оставляя обычные (на случай, если Telegram их не принял).
    Значок из нескольких эмодзи подряд (WIN) превращается в один обычный."""
    return re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>(?:<tg-emoji emoji-id="\d+">\1</tg-emoji>)*',
                  r"\1", html)


load()
