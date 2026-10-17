"""Разбор матча OpenDota в удобную структуру + определение позиций и ранга."""
import logging
from dataclasses import dataclass, field

from api import Api, ApiError

log = logging.getLogger(__name__)

# названия медалей как в русском клиенте Dota 2
RANK_NAMES = {1: "Рекрут", 2: "Страж", 3: "Рыцарь", 4: "Герой",
              5: "Легенда", 6: "Властелин", 7: "Божество", 8: "Титан"}
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}
POSITION_NAMES = {1: "Керри", 2: "Мидер", 3: "Хардлейнер", 4: "Саппорт 4", 5: "Саппорт 5"}
LOBBY_NAMES = {0: "Обычная", 7: "Рейтинговая", 1: "Тренировочная", 2: "Турнирная", 4: "С ботами"}
MODE_NAMES = {1: "All Pick", 2: "Captains Mode", 3: "Random Draft", 4: "Single Draft", 5: "All Random",
              18: "Ability Draft", 22: "All Pick", 23: "Turbo"}


def rank_name(rank_tier: int | None, leaderboard: int | None = None) -> str:
    if not rank_tier:
        return "Без ранга"
    medal, stars = divmod(int(rank_tier), 10)
    name = RANK_NAMES.get(medal, "?")
    if medal == 8:
        return f"{name} #{leaderboard}" if leaderboard else name
    return f"{name} {ROMAN.get(stars, '')}".strip()


def mmr_to_rank_tier(mmr: int | None) -> int | None:
    """MMR -> медаль (rank_tier: десятки — медаль, единицы — звёзды).

    Рекрут–Властелин: 770 MMR на медаль, 154 на звезду; Божество: 200 на звезду с 4620; Титан от 5620.
    """
    if not mmr:
        return None
    if mmr >= 5620:
        return 80
    if mmr >= 4620:
        return 70 + min(5, (mmr - 4620) // 200 + 1)
    medal, rest = divmod(int(mmr), 770)
    return (medal + 1) * 10 + min(5, rest // 154 + 1)


def bracket_of(rank_tier: int | None) -> str | None:
    """Ранг -> базовый брекет STRATZ."""
    if not rank_tier:
        return None
    medal = int(rank_tier) // 10
    return {1: "HERALD_GUARDIAN", 2: "HERALD_GUARDIAN", 3: "CRUSADER_ARCHON", 4: "CRUSADER_ARCHON",
            5: "LEGEND_ANCIENT", 6: "LEGEND_ANCIENT", 7: "DIVINE_IMMORTAL", 8: "DIVINE_IMMORTAL"}.get(medal)


BRACKET_NAMES = {"HERALD_GUARDIAN": "Рекрут–Страж", "CRUSADER_ARCHON": "Рыцарь–Герой",
                 "LEGEND_ANCIENT": "Легенда–Властелин", "DIVINE_IMMORTAL": "Божество–Титан"}


@dataclass
class Player:
    slot: int
    is_radiant: bool
    account_id: int | None
    name: str
    hero_id: int
    hero_name: str
    kills: int
    deaths: int
    assists: int
    gpm: int
    xpm: int
    lh: int
    dn: int
    nw: int
    level: int
    nw10: int | None
    lh10: int | None
    items: list[int]
    neutral: int | None
    enhancement: int | None
    rank_tier: int | None
    lane_role: int | None
    benchmarks: dict
    leaver: bool = False        # вылетел/покинул игру — его фарм не отражает роль
    hero_roles: tuple = ()
    position: int | None = None
    position_exact: bool = False

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(self.deaths, 1)


@dataclass
class Match:
    match_id: int
    start_time: int
    duration: int
    radiant_win: bool
    radiant_score: int
    dire_score: int
    lobby_type: int
    game_mode: int
    parsed: bool
    players: list[Player]
    bans: dict[bool, list[int]] = field(default_factory=dict)   # is_radiant -> hero ids
    avg_rank_tier: int | None = None

    @property
    def is_ranked(self):
        return self.lobby_type == 7

    def find(self, account_id: int) -> Player | None:
        return next((p for p in self.players if p.account_id == account_id), None)

    def team(self, radiant: bool) -> list[Player]:
        return sorted([p for p in self.players if p.is_radiant == radiant], key=lambda p: (p.position or 9, p.slot))


def parse_match(data: dict, api: Api) -> Match:
    players = []
    for p in data.get("players", []):
        hero = api.heroes.get(p.get("hero_id"), {})
        gold_t = p.get("gold_t") or []
        lh_t = p.get("lh_t") or []
        players.append(Player(
            slot=p.get("player_slot", 0),
            is_radiant=p.get("player_slot", 0) < 128,
            account_id=p.get("account_id"),
            name=p.get("personaname") or "Аноним",
            hero_id=p.get("hero_id", 0),
            hero_name=hero.get("localized_name", f"Hero {p.get('hero_id')}"),
            kills=p.get("kills", 0), deaths=p.get("deaths", 0), assists=p.get("assists", 0),
            gpm=p.get("gold_per_min", 0), xpm=p.get("xp_per_min", 0),
            lh=p.get("last_hits", 0), dn=p.get("denies", 0),
            nw=p.get("net_worth") or p.get("total_gold") or 0,
            level=p.get("level", 0),
            nw10=gold_t[10] if len(gold_t) > 10 else None,
            lh10=lh_t[10] if len(lh_t) > 10 else None,
            items=[p.get(f"item_{i}", 0) for i in range(6)],
            neutral=p.get("item_neutral") or None,
            enhancement=p.get("item_neutral2") or None,
            rank_tier=p.get("rank_tier"),
            lane_role=p.get("lane_role"),
            benchmarks=p.get("benchmarks") or {},
            leaver=(p.get("leaver_status") or 0) > 0,
            hero_roles=tuple(hero.get("roles") or ()),
        ))

    bans = {True: [], False: []}
    for pb in data.get("picks_bans") or []:
        if not pb.get("is_pick"):
            bans[pb.get("team") == 0].append(pb["hero_id"])

    ranks = [p.rank_tier for p in players if p.rank_tier]
    avg_rank = round(sum(ranks) / len(ranks)) if ranks else None

    m = Match(
        match_id=data["match_id"], start_time=data.get("start_time", 0), duration=data.get("duration", 0),
        radiant_win=bool(data.get("radiant_win")), radiant_score=data.get("radiant_score", 0),
        dire_score=data.get("dire_score", 0), lobby_type=data.get("lobby_type", 0),
        game_mode=data.get("game_mode", 0),
        parsed=bool(data.get("version")) or bool((data.get("od_data") or {}).get("has_parsed")),
        players=players, bans=bans, avg_rank_tier=avg_rank,
    )
    guess_positions(m)
    return m


SAFE, MID, OFF = 1, 2, 3   # lane_role из OpenDota
# кор и саппорт каждой линии: лёгкая — 1 и 5, мид — 2, сложная — 3 и 4
LANE_POS = {SAFE: (1, 5), MID: (2, None), OFF: (3, 4)}


def _farm(p: Player) -> float:
    """Фарм для сравнения внутри линии: золото на 10-й минуте (стадия линий),
    а не итоговый нетворс, который ломается, если игрок вылетел позже."""
    if p.leaver:
        # вылетевший сравнивается по роли героя: керри выше саппорта
        return 1e9 if "Carry" in p.hero_roles else (-1 if "Support" in p.hero_roles else 0)
    if p.nw10 is not None:
        return p.nw10
    return p.lh10 if p.lh10 is not None else p.nw


def guess_positions(m: Match):
    """Позиции строго по линиям: на каждой линии сравниваются только стоявшие на ней.

    Лёгкая: больше фарм на 10-й минуте — pos 1, меньше — pos 5. Сложная: 3 и 4.
    Мид — всегда pos 2. Без разбора реплея линий нет — позицию не угадываем
    (её может проставить STRATZ).
    """
    for radiant in (True, False):
        team = [p for p in m.players if p.is_radiant == radiant]
        if not any(p.lane_role in LANE_POS for p in team):
            continue
        free = {1, 2, 3, 4, 5}
        rest = []
        for lane, (core, support) in LANE_POS.items():
            ps = sorted((p for p in team if p.lane_role == lane), key=_farm, reverse=True)
            if not ps:
                continue
            roles = [core, support] if support else [core]
            for p, pos in zip(ps, roles):
                p.position = pos
                free.discard(pos)
            rest.extend(ps[len(roles):])
        # трилайн, роумеры, лес: оставшиеся позиции по убыванию фарма
        rest += [p for p in team if p.lane_role not in LANE_POS]
        rest.sort(key=_farm, reverse=True)
        for p, pos in zip(rest, sorted(free)):
            p.position = pos


async def apply_stratz_positions(m: Match, api: Api):
    """Позиции из STRATZ для тех, кому не нашлось позиции по линиям (матч не разобран)."""
    if not api.has_stratz:
        return
    try:
        data = await api.stratz(
            f"{{ match(id: {m.match_id}) {{ players {{ heroId position }} }} }}"
        )
    except ApiError as e:
        log.info("STRATZ positions: %s", e)
        return
    players = ((data or {}).get("match") or {}).get("players") or []
    by_hero = {p["heroId"]: p.get("position") for p in players}
    for p in m.players:
        if p.position:   # позиции по линиям из разбора важнее: STRATZ тоже смотрит на фарм
            continue
        pos = by_hero.get(p.hero_id)
        if pos and pos.startswith("POSITION_"):
            p.position = int(pos[-1])
            p.position_exact = True
