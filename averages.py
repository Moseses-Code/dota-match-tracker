"""Средние показатели героя на позиции и ранге (STRATZ heroStats).

STRATZ отдаёт агрегаты по (герой, позиция, брекет). Обычный блок `stats` - по итогам игры,
с groupByTime - по минутам. Значения в некоторых версиях API приходят суммой по всем матчам,
поэтому ниже есть нормализация: если число явно не похоже на среднее, делим на matchCount.
Проверить, что приходит на самом деле, можно скриптом check_stratz.py.
"""
import json
import logging

from analysis import bracket_of
from api import Api, ApiError
from db import DB

log = logging.getLogger(__name__)

FIELDS = "matchCount kills deaths assists goldPerMinute xp cs dn networth"
CACHE_AGE = 12 * 3600
# STRATZ не отдаёт средний GPM. Оцениваем его как нетворс/минуты × коэффициент позиции:
# коэффициенты посчитаны по 200 игрокам из рейтинговых матчей (саппорты тратят больше на расходники).
GPM_FACTOR = {1: 1.05, 2: 1.05, 3: 1.06, 4: 1.15, 5: 1.23}


def _q(hero_id, position, bracket, by_time: bool):
    args = [f"heroIds: [{hero_id}]"]
    if position:
        args.append(f"positionIds: [POSITION_{position}]")
    if bracket:
        args.append(f"bracketBasicIds: [{bracket}]")
    args.append(f"groupByTime: {'true' if by_time else 'false'}")
    if by_time:
        args.append("minTime: 0, maxTime: 90")
    return f"stats({', '.join(args)}) {{ time {FIELDS} }}"


async def fetch_raw(api: Api, hero_id, position, bracket):
    query = ("{ heroStats { overall: " + _q(hero_id, position, bracket, False)
             + " timeline: " + _q(hero_id, position, bracket, True) + " } }")
    data = await api.stratz(query)
    return data["heroStats"]


def _normalize(row: dict) -> dict:
    """Приводит строку к средним за матч."""
    n = row.get("matchCount") or 1
    looks_summed = n > 1 and (
        (row.get("kills") or 0) > 80 or (row.get("networth") or 0) > 300_000
    )
    out = {}
    for k, v in row.items():
        if k in ("matchCount", "time") or v is None:
            out[k] = v
        else:
            out[k] = v / n if looks_summed else v
    return out


def _cumulative(rows: list[dict], key: str) -> list[float]:
    """Если ряд не растёт - значит это прирост за минуту, суммируем."""
    vals = [r.get(key) or 0 for r in rows]
    if len(vals) > 3 and sum(1 for a, b in zip(vals, vals[1:]) if b < a) > len(vals) // 3:
        acc, out = 0, []
        for v in vals:
            acc += v
            out.append(acc)
        return out
    return vals


def derive(raw: dict, duration_min: float, position: int | None = None) -> dict | None:
    overall = [_normalize(r) for r in (raw.get("overall") or [])]
    timeline = sorted((_normalize(r) for r in (raw.get("timeline") or []) if r.get("time") is not None),
                      key=lambda r: r["time"])
    if not overall and not timeline:
        return None
    o = overall[0] if overall else {}
    res = {"matches": o.get("matchCount"), "kills": o.get("kills"), "deaths": o.get("deaths"),
           "assists": o.get("assists"), "gpm": o.get("goldPerMinute")}
    if res["kills"] is not None and res["deaths"] is not None:
        res["kda"] = (res["kills"] + (res["assists"] or 0)) / max(res["deaths"], 1)

    if timeline:
        if max(r["time"] for r in timeline) > 200:          # секунды -> минуты
            for r in timeline:
                r["time"] = r["time"] / 60
        times = [r["time"] for r in timeline]
        xp = _cumulative(timeline, "xp")
        cs = _cumulative(timeline, "cs")
        dn = _cumulative(timeline, "dn")
        nw = [r.get("networth") for r in timeline]

        def at(minute):
            return min(range(len(times)), key=lambda i: abs(times[i] - minute))

        i_end, i10 = at(duration_min), at(10)
        if times[i_end] > 0:
            res["xpm"] = xp[i_end] / times[i_end]
        res["lh"], res["dn"], res["nw"] = cs[i_end], dn[i_end], nw[i_end]
        if not res.get("gpm") and nw[i_end] and times[i_end] > 0:
            res["gpm"] = nw[i_end] / times[i_end] * GPM_FACTOR.get(position, 1.1)
            res["gpm_estimated"] = True
        if abs(times[i10] - 10) <= 1:
            res["nw10"], res["lh10"] = nw[i10], cs[i10]
    else:
        res["lh"], res["dn"], res["nw"] = o.get("cs"), o.get("dn"), o.get("networth")

    # отсекаем явный мусор, чтобы не показывать бессмыслицу
    if res.get("xpm") and not 50 < res["xpm"] < 3000:
        res["xpm"] = None
    if res.get("gpm") and not 50 < res["gpm"] < 2000:
        res["gpm"] = None
    return res


async def get_averages(api: Api, db: DB, hero_id: int, position: int | None, rank_tier: int | None,
                       duration_sec: int) -> dict | None:
    if not api.has_stratz:
        return None
    bracket = bracket_of(rank_tier)
    key = f"{hero_id}:{position}:{bracket}"
    cached = await db.get_cache(key, CACHE_AGE)
    if cached:
        raw = json.loads(cached)
    else:
        try:
            raw = await fetch_raw(api, hero_id, position, bracket)
        except ApiError as e:
            log.warning("STRATZ averages: %s", e)
            return None
        await db.set_cache(key, json.dumps(raw))
    res = derive(raw, duration_sec / 60, position)
    if res:
        res["bracket"] = bracket
    return res
