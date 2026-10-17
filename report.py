"""Текст отчёта о матче (HTML для Telegram). Идёт подписью к картинке драфта — одним сообщением."""
from dataclasses import dataclass
from html import escape

from analysis import BRACKET_NAMES, MODE_NAMES, POSITION_NAMES, Match, Player, rank_name
from api import Api
from emoji import e, hero_e, item_e, rank_e


@dataclass
class MmrInfo:
    ranked: bool
    delta: int | None = None       # ±шаг
    current: int | None = None     # если игрок указал свой MMR
    old_rank: int | None = None
    new_rank: int | None = None
    leaderboard: int | None = None


def fmt_time(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_num(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 10000:
        return f"{v / 1000:.1f}k"
    return f"{v:,.0f}".replace(",", " ")


def diff(mine: float | None, avg: float | None) -> str:
    """▲12% / ▼8% / ≈ к среднему."""
    if mine is None or not avg:
        return ""
    d = (mine - avg) / avg * 100
    if abs(d) < 3:
        return "  <i>≈</i>"
    return f"  <b>{'▲' if d > 0 else '▼'}{abs(d):.0f}%</b>"


def bench(p: Player, key: str) -> str:
    """Без STRATZ: процентиль OpenDota среди игроков на этом герое."""
    pct = ((p.benchmarks or {}).get(key) or {}).get("pct")
    return f"  <i>топ {100 - round(pct * 100)}%</i>" if pct is not None else ""


def line(icon: str, title: str, value: str, mine, avg, avg_txt=None, fallback="") -> str:
    out = f"{icon} {title} <b>{value}</b>"
    if avg:
        out += f" <i>/ {avg_txt or fmt_num(avg)}</i>{diff(mine, avg)}"
    else:
        out += fallback
    return out


def build_line(p: Player, api: Api) -> str:
    """Финальный билд иконками предметов (если иконок ещё нет — названиями)."""
    main = [i for i in p.items if i and i in api.items]
    extra = [i for i in (p.neutral, p.enhancement) if i and i in api.items]

    def show(ids):
        icons = [item_e(api.items[i]["name"]) for i in ids]
        if all(icons):
            return " ".join(icons)
        return ", ".join(escape(api.items[i]["dname"]) for i in ids)

    out = show(main) if main else "—"
    if extra:
        out += "  ┃  " + show(extra)
    return out


def build_report(m: Match, me: Player, avg: dict | None, mmr: MmrInfo, api: Api) -> str:
    won = me.is_radiant == m.radiant_win
    a = avg or {}

    # исход
    mode = MODE_NAMES.get(m.game_mode, "Матч")
    kind = "Рейтинг" if m.is_ranked else "Обычная"
    head = f"{e('win') if won else e('lose')} <b>{'ПОБЕДА' if won else 'ПОРАЖЕНИЕ'}</b>  ·  {fmt_time(m.duration)}"
    winner = "Сил Света" if m.radiant_win else "Сил Тьмы"
    score = (f"{e('radiant')} <b>{m.radiant_score}</b> : <b>{m.dire_score}</b> {e('dire')}  ·  "
             f"<i>победа {winner}</i>")
    sub = f"<i>{kind} · {mode}</i>"

    # герой и позиция
    pos = me.position
    role = f"{POSITION_NAMES[pos]} (pos {pos})" if pos else "позиция не определена"
    hero = f"{hero_e(me.hero_id)} <b>{escape(me.hero_name)}</b> · {role}"

    # ранг и MMR
    rank = f"{rank_e(mmr.new_rank)} <b>{rank_name(mmr.new_rank, mmr.leaderboard)}</b>"
    if mmr.current:
        rank += f" · {fmt_num(mmr.current)} MMR"
    if mmr.ranked and mmr.delta is not None:
        rank += f" <b>({'+' if mmr.delta > 0 else '−'}{abs(mmr.delta)})</b>"
    if mmr.old_rank and mmr.new_rank and mmr.old_rank != mmr.new_rank:
        rank += f"  {e('new_rank')}" if mmr.new_rank > mmr.old_rank else f"  {e('mmr_down')}"

    # статистика: моё / среднее  ▲%
    gpm_avg = ("≈" + fmt_num(a["gpm"])) if a.get("gpm_estimated") and a.get("gpm") else None
    stats = [
        line(e("kda"), "KDA", f"{me.kills}/{me.deaths}/{me.assists}", me.kda, a.get("kda"),
             f"{a['kda']:.1f}" if a.get("kda") else None),
        line(e("gpm"), "GPM", str(me.gpm), me.gpm, a.get("gpm"), gpm_avg, bench(me, "gold_per_min")),
        line(e("xpm"), "XPM", str(me.xpm), me.xpm, a.get("xpm"), fallback=bench(me, "xp_per_min")),
        line(e("creeps"), "Крипы", f"{me.lh}/{me.dn}", me.lh, a.get("lh"),
             f"{fmt_num(a.get('lh'))}/{fmt_num(a.get('dn'))}" if a.get("lh") else None,
             bench(me, "last_hits_per_min")),
        line(e("nw"), "Нетворс", fmt_num(me.nw), me.nw, a.get("nw")),
    ]
    if me.nw10 is not None:
        stats.append(line(e("nw10"), "NW 10'", fmt_num(me.nw10), me.nw10, a.get("nw10")))
    if avg:
        legend = (f"<i>моё / среднее · {escape(me.hero_name)} pos {pos} · "
                  f"{BRACKET_NAMES.get(a.get('bracket'), 'все ранги')}</i>")
    else:
        legend = "<i>топ X% — среди игроков на этом герое</i>"

    return "\n".join([
        head, score, sub, "",
        hero, rank, "",
        "<blockquote>" + "\n".join(stats) + "</blockquote>",
        legend, "",
        build_line(me, api),
    ])


def short_caption(m: Match, me: Player) -> str:
    won = me.is_radiant == m.radiant_win
    return (f"{e('win') if won else e('lose')} <b>{'Победа' if won else 'Поражение'}</b> · "
            f"{hero_e(me.hero_id)} {escape(me.hero_name)} · {fmt_time(m.duration)}")
