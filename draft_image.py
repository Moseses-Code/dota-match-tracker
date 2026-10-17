"""Картинка драфта матча в стиле турнирных трансляций."""
import asyncio
import io
import logging

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from analysis import Match, Player
from api import Api
from config import CACHE_DIR

log = logging.getLogger(__name__)

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf"
FONT_PATH = CACHE_DIR / "Montserrat.ttf"

W = 1280
MARGIN = 48
CARD_W, CARD_H = 224, 126
GAP = (W - 2 * MARGIN - 5 * CARD_W) // 4
BAN_W, BAN_H = 72, 40

BG_TOP, BG_BOTTOM = (13, 17, 26), (24, 28, 40)
RADIANT = (102, 187, 106)
DIRE = (229, 83, 72)
GOLD = (255, 196, 64)
TEXT = (235, 238, 245)
MUTED = (140, 148, 165)
CARD_BG = (32, 37, 52)


async def ensure_font(api: Api):
    if not FONT_PATH.exists():
        try:
            await api.download(FONT_URL, FONT_PATH)
        except Exception as e:  # noqa: BLE001
            log.warning("font download failed: %s", e)


_font_cache: dict = {}


def font(size: int, weight: int = 600):
    key = (size, weight)
    if key in _font_cache:
        return _font_cache[key]
    f = None
    if FONT_PATH.exists():
        try:
            f = ImageFont.truetype(str(FONT_PATH), size)
            try:
                f.set_variation_by_axes([weight])
            except Exception:  # noqa: BLE001
                pass
        except OSError:
            f = None
    if f is None:
        for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
    if f is None:
        f = ImageFont.load_default(size)
    _font_cache[key] = f
    return f


def _open(data: bytes | None, size) -> Image.Image:
    if data:
        try:
            return ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGBA"), size, Image.LANCZOS)
        except Exception:  # noqa: BLE001
            pass
    return Image.new("RGBA", size, CARD_BG)


def _fit_text(draw, text, fnt, max_w):
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text + "…"


def _gradient(w, h):
    img = Image.new("RGB", (w, h), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    return img


def _round_mask(size, r):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], r, fill=255)
    return mask


INFO_H = 66   # подпись под портретом: герой + ник


def render(m: Match, me_id: int | None, hero_imgs: dict) -> bytes:
    me = m.find(me_id) if me_id else None
    team_h = 60 + CARD_H + INFO_H
    H = 120 + team_h * 2 + 30 + 36
    img = _gradient(W, H)
    d = ImageDraw.Draw(img, "RGBA")

    # --- шапка: счёт, победитель, длительность ---
    winner = "СИЛЫ СВЕТА" if m.radiant_win else "СИЛЫ ТЬМЫ"
    wc = RADIANT if m.radiant_win else DIRE
    d.text((W / 2 - 28, 40), str(m.radiant_score), font=font(46, 800), fill=RADIANT, anchor="rm")
    d.text((W / 2, 38), ":", font=font(46, 800), fill=MUTED, anchor="mm")
    d.text((W / 2 + 28, 40), str(m.dire_score), font=font(46, 800), fill=DIRE, anchor="lm")
    mins, secs = divmod(m.duration, 60)
    d.text((W / 2, 82), f"ПОБЕДА: {winner}  ·  {mins}:{secs:02d}", font=font(20, 600), fill=wc, anchor="mm")
    d.text((MARGIN, 38), f"MATCH {m.match_id}", font=font(16, 500), fill=MUTED, anchor="lm")
    d.line([(MARGIN, 108), (W - MARGIN, 108)], fill=(50, 56, 74), width=2)

    y = 120
    for radiant in (True, False):
        _team(img, d, m, radiant, y, me, hero_imgs)
        y += team_h + 30

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _team(img, d, m: Match, radiant: bool, y: int, me: Player | None, hero_imgs):
    color = RADIANT if radiant else DIRE
    won = m.radiant_win == radiant
    title = "СИЛЫ СВЕТА" if radiant else "СИЛЫ ТЬМЫ"
    d.rounded_rectangle([MARGIN, y + 8, MARGIN + 6, y + 40], 3, fill=color)
    d.text((MARGIN + 18, y + 24), title, font=font(26, 800), fill=color, anchor="lm")
    if won:
        tw = d.textlength(title, font=font(26, 800))
        d.text((MARGIN + 34 + tw, y + 24), "ПОБЕДА", font=font(15, 700), fill=GOLD, anchor="lm")

    # баны справа
    bans = m.bans.get(radiant, [])[:7]
    bx = W - MARGIN - len(bans) * (BAN_W + 6) + 6
    if bans:
        d.text((bx - 12, y + 24), "БАНЫ", font=font(14, 600), fill=MUTED, anchor="rm")
    for hid in bans:
        ban = ImageOps.grayscale(_open(hero_imgs.get(hid), (BAN_W, BAN_H)).convert("RGB")).convert("RGBA")
        ban = Image.blend(ban, Image.new("RGBA", ban.size, (20, 20, 20, 255)), 0.35)
        bd = ImageDraw.Draw(ban)
        bd.line([(4, BAN_H - 4), (BAN_W - 4, 4)], fill=(220, 60, 60), width=3)
        img.paste(ban, (bx, y + 4), _round_mask((BAN_W, BAN_H), 5))
        bx += BAN_W + 6

    # карточки героев
    cy = y + 60
    for i, p in enumerate(m.team(radiant)):
        cx = MARGIN + i * (CARD_W + GAP)
        is_me = me is not None and p.account_id == me.account_id
        if is_me:   # свечение вокруг своей карточки
            glow = Image.new("RGBA", (CARD_W + 40, CARD_H + INFO_H + 40), (0, 0, 0, 0))
            ImageDraw.Draw(glow).rounded_rectangle([20, 20, CARD_W + 20, CARD_H + INFO_H + 20], 14, fill=(*GOLD, 140))
            glow = glow.filter(ImageFilter.GaussianBlur(10))
            img.paste(glow, (cx - 20, cy - 20), glow)
        d.rounded_rectangle([cx, cy, cx + CARD_W, cy + CARD_H + INFO_H], 12, fill=CARD_BG,
                            outline=GOLD if is_me else None, width=3 if is_me else 0)
        portrait = _open(hero_imgs.get(p.hero_id), (CARD_W, CARD_H))
        mask = Image.new("L", (CARD_W, CARD_H), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, CARD_W - 1, CARD_H + 20], 12, fill=255)
        img.paste(portrait, (cx, cy), mask)
        if is_me:
            d.rounded_rectangle([cx + 2, cy + CARD_H - 2, cx + CARD_W - 2, cy + CARD_H + 2], 0, fill=GOLD)

        # бейдж позиции
        if p.position:
            d.rounded_rectangle([cx + 8, cy + 8, cx + 62, cy + 32], 6, fill=(0, 0, 0, 180))
            d.text((cx + 35, cy + 20), f"POS {p.position}", font=font(13, 700), fill=TEXT, anchor="mm")
        if is_me:
            d.rounded_rectangle([cx + CARD_W - 48, cy + 8, cx + CARD_W - 8, cy + 32], 6, fill=GOLD)
            d.text((cx + CARD_W - 28, cy + 20), "ТЫ", font=font(13, 800), fill=(20, 20, 20), anchor="mm")

        ty = cy + CARD_H + 12
        d.text((cx + 12, ty), _fit_text(d, p.hero_name.upper(), font(16, 800), CARD_W - 24),
               font=font(16, 800), fill=GOLD if is_me else TEXT)
        d.text((cx + 12, ty + 24), _fit_text(d, p.name, font(14, 500), CARD_W - 24),
               font=font(14, 500), fill=MUTED)


async def build_draft_image(m: Match, me_id: int | None, api: Api) -> bytes:
    await ensure_font(api)
    hero_ids = list({p.hero_id for p in m.players} | set(m.bans.get(True, [])) | set(m.bans.get(False, [])))
    hero_data = await asyncio.gather(*(api.hero_image(h) for h in hero_ids))
    return await asyncio.to_thread(render, m, me_id, dict(zip(hero_ids, hero_data)))
