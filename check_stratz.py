"""Проверка STRATZ-токена и того, какие средние приходят.

Запуск:  python check_stratz.py 74 2 65
         (герой Invoker = 74, позиция 2, ранг 65 = Властелин V; ID героев - в data/cache/heroes.json)
"""
import asyncio
import json
import sys

from analysis import bracket_of
from api import Api
from averages import derive, fetch_raw


async def main():
    hero = int(sys.argv[1]) if len(sys.argv) > 1 else 74
    pos = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    rank = int(sys.argv[3]) if len(sys.argv) > 3 else 65
    api = Api()
    await api.open()
    try:
        raw = await fetch_raw(api, hero, pos, bracket_of(rank))
        print("=== overall ===")
        print(json.dumps(raw.get("overall"), indent=1, ensure_ascii=False)[:1500])
        print("=== timeline (первые 3 и минута 10) ===")
        tl = raw.get("timeline") or []
        print(json.dumps(tl[:3] + [r for r in tl if r.get("time") == 10], indent=1)[:2000])
        print("=== что увидит бот для игры на 35 минут ===")
        print(json.dumps(derive(raw, 35, pos), indent=1, ensure_ascii=False))
    finally:
        await api.close()


asyncio.run(main())
