"""Подробная статистика матча (кнопка «Подробнее»): картинка-«паутинка» вклада + подпись."""
import asyncio
import io
import math
from html import escape

from PIL import Image, ImageDraw

from analysis import POSITION_NAMES, Match, Player
from api import Api
from draft_image import BG_BOTTOM, BG_TOP, CARD_BG, GOLD, MUTED, TEXT, ensure_font, font
from report import fmt_num, fmt_time

AXES = ["Фарм", "Драки", "Строения", "Лечение", "Саппортинг"]
GRID = (52, 58, 78)
W, H = 1280, 720
K = 2                    # рисуем в 2 раза крупнее и уменьшаем — так линии сглажены
OPPONENT = {1: 3, 2: 2, 3: 1, 4: 5, 5: 4}   # кто стоит напротив на линии
BIG_ITEM_COST = 1800     # тайминги показываем только для дорогих предметов


def _share(mine: float, team: list[float]) -> float | None:
    total = sum(team)
    return mine / total * 100 if total > 0 else None


def contribution(data: dict, m: Match, me: Player) -> dict[str, float]:
    """Доли игрока от команды, %: фарм, драки, строения, лечение, саппортинг."""
    raw = [p for p in data.get("players", []) if (p.get("player_slot", 0) < 128) == me.is_radiant]
    mine = next((p for p in raw if p.get("player_slot") == me.slot), {})

    def share(key):
        return _share(mine.get(key) or 0, [p.get(key) or 0 for p in raw])

    team_kills = sum(p.get("kills") or 0 for p in raw)
    fights = mine.get("teamfight_participation")
    if fights is None and team_kills:
        fights = ((mine.get("kills") or 0) + (mine.get("assists") or 0)) / team_kills
    # саппортинг: среднее из долей по вардам, стакам и оглушениям (что есть в матче)
    support = [s for s in (
        _share((mine.get("obs_placed") or 0) + (mine.get("sen_placed") or 0),
               [(p.get("obs_placed") or 0) + (p.get("sen_placed") or 0) for p in raw]),
        share("camps_stacked"),
        share("stuns"),
    ) if s is not None]
    return {
        "Фарм": _share(me.nw, [p.nw for p in m.players if p.is_radiant == me.is_radiant]) or 0,
        "Драки": min((fights or 0) * 100, 100),
        "Строения": share("tower_damage") or 0,
        "Лечение": share("hero_healing") or 0,
        "Саппортинг": sum(support) / len(support) if support else 0,
    }


def details_caption(data: dict, m: Match, me: Player, api: Api) -> str:
    mine = next((p for p in data.get("players", []) if p.get("player_slot") == me.slot), {})
    lines = []
    taken = sum((mine.get("damage_taken") or {}).values())
    lines.append(f"⚔️ Урон по героям <b>{fmt_num(mine.get('hero_damage'))}</b>"
                 f" · по строениям <b>{fmt_num(mine.get('tower_damage'))}</b>")
    lines.append(f"💚 Лечение <b>{fmt_num(mine.get('hero_healing'))}</b>"
                 + (f" · получено урона <b>{fmt_num(taken)}</b>" if taken else ""))
    fights = mine.get("teamfight_participation")
    stuns = mine.get("stuns")
    parts = []
    if fights is not None:
        parts.append(f"участие в драках <b>{fights * 100:.0f}%</b>")
    if stuns:
        parts.append(f"оглушения <b>{stuns:.1f} с</b>")
    if parts:
        text = " · ".join(parts)
        lines.append("🤝 " + text[0].upper() + text[1:])
    obs, sen, stacks = mine.get("obs_placed"), mine.get("sen_placed"), mine.get("camps_stacked")
    if obs is not None or sen is not None:
        lines.append(f"👁 Варды <b>{obs or 0}</b> / сентри <b>{sen or 0}</b> · стаки <b>{stacks or 0}</b>")

    # итог линии: золото на 10-й минуте против прямого оппонента
    opp = next((p for p in m.players if p.is_radiant != me.is_radiant and me.position
                and p.position == OPPONENT.get(me.position)), None)
    if me.nw10 is not None and opp and opp.nw10 is not None:
        d = me.nw10 - opp.nw10
        lines.append(f"🛣 Линия: NW 10' <b>{fmt_num(me.nw10)}</b> против <b>{fmt_num(opp.nw10)}</b>"
                     f" ({escape(opp.hero_name)}) <b>{'+' if d >= 0 else '−'}{fmt_num(abs(d))}</b>")

    timings = _item_timings(mine, api)
    if timings:
        lines.append("⏱ " + " · ".join(f"{escape(n)} {fmt_time(t)}" for n, t in timings))

    extra = []
    multi = {int(k): v for k, v in (mine.get("multi_kills") or {}).items() if int(k) >= 2}
    names = {2: "двойное", 3: "тройное", 4: "ультра", 5: "rampage"}
    if multi:
        extra.append(", ".join(f"{names.get(k, f'{k}×')} ×{v}" for k, v in sorted(multi.items())))
    if mine.get("life_state_dead"):
        extra.append(f"в смерти {fmt_time(mine['life_state_dead'])}")
    if mine.get("buyback_count"):
        extra.append(f"выкупов {mine['buyback_count']}")
    if extra:
        text = " · ".join(extra)
        lines.append("🔥 " + text[0].upper() + text[1:])
    return "\n".join(lines)


def _item_timings(raw: dict, api: Api, limit: int = 6) -> list[tuple[str, int]]:
    """Первые покупки дорогих предметов из финального билда."""
    by_name = {it["name"]: it for it in api.items.values()}
    final = {api.items[i]["name"] for i in
             [raw.get(f"item_{k}") for k in range(6)] + [raw.get(f"backpack_{k}") for k in range(3)]
             if i and i in api.items}
    seen, out = set(), []
    for buy in raw.get("purchase_log") or []:
        key, t = buy.get("key"), buy.get("time", 0)
        it = by_name.get(key)
        if not it or key in seen or key not in final or t < 0 or it.get("cost", 0) < BIG_ITEM_COST:
            continue
        seen.add(key)
        out.append((it["dname"], t))
    return out[:limit]


def render(shares: dict[str, float], title: str, subtitle: str) -> bytes:
    img = Image.new("RGB", (W * K, H * K))
    d = ImageDraw.Draw(img)
    for y in range(H * K):
        t = y / (H * K)
        d.line([(0, y), (W * K, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))

    def f(size, weight=700):
        return font(size * K, weight)

    vals = [shares[a] for a in AXES]
    scale = max(50, math.ceil(max(vals) / 10) * 10)   # край «паутинки» в процентах
    cx, cy, r = 410 * K, 410 * K, 205 * K

    def pt(i, v):
        a = -math.pi / 2 + 2 * math.pi * i / len(AXES)
        return cx + r * v / scale * math.cos(a), cy + r * v / scale * math.sin(a)

    for lvl in (0.25, 0.5, 0.75, 1):
        d.polygon([pt(i, scale * lvl) for i in range(len(AXES))], outline=GRID, width=2 * K)
    for i in range(len(AXES)):
        d.line([(cx, cy), pt(i, scale)], fill=GRID, width=2 * K)

    pts = [pt(i, v) for i, v in enumerate(vals)]
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).polygon(pts, fill=GOLD + (70,))
    img.paste(layer, (0, 0), layer)
    d = ImageDraw.Draw(img)
    d.line(pts + [pts[0]], fill=GOLD, width=4 * K, joint="curve")
    for x, y in pts:
        d.ellipse([x - 7 * K, y - 7 * K, x + 7 * K, y + 7 * K], fill=GOLD)
    for i, name in enumerate(AXES):
        x, y = pt(i, scale * 1.3)
        d.text((x, y - 12 * K), name, font=f(24), fill=TEXT, anchor="mm")
        d.text((x, y + 16 * K), f"{vals[i]:.0f}%", font=f(20), fill=GOLD, anchor="mm")

    d.text((48 * K, 40 * K), title, font=f(36, 800), fill=TEXT)
    d.text((48 * K, 88 * K), subtitle, font=f(20, 500), fill=MUTED)

    # справа те же доли полосками
    x0, y0, x1 = 800 * K, 150 * K, (W - 72) * K
    d.rounded_rectangle([x0 - 24 * K, y0 - 28 * K, (W - 48) * K, y0 + 470 * K], radius=18 * K, fill=CARD_BG)
    d.text((x0, y0), "Доля от команды", font=f(22), fill=TEXT)
    for i, name in enumerate(AXES):
        yy = y0 + (60 + i * 78) * K
        d.text((x0, yy), name, font=f(20), fill=TEXT)
        d.text((x1, yy), f"{vals[i]:.0f}%", font=f(20), fill=GOLD, anchor="ra")
        d.rounded_rectangle([x0, yy + 32 * K, x1, yy + 44 * K], radius=6 * K, fill=GRID)
        if vals[i] > 0:
            d.rounded_rectangle([x0, yy + 32 * K, x0 + max((x1 - x0) * vals[i] / 100, 12 * K), yy + 44 * K],
                                radius=6 * K, fill=GOLD)

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


async def build_details(data: dict, m: Match, me: Player, api: Api) -> tuple[bytes, str]:
    await ensure_font(api)
    won = me.is_radiant == m.radiant_win
    role = POSITION_NAMES.get(me.position, "")
    subtitle = " · ".join(x for x in (me.hero_name, f"{role} (pos {me.position})" if role else "",
                                      f"{'ПОБЕДА' if won else 'ПОРАЖЕНИЕ'} {fmt_time(m.duration)}") if x)
    png = await asyncio.to_thread(render, contribution(data, m, me), "Вклад в игру", subtitle)
    return png, details_caption(data, m, me, api)
