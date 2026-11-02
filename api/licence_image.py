"""The AI Driving License as a PNG (design E:\\licence_latest.png), drawn with
Pillow so Slack can show it in an image block. The website draws the same
design as SVG (aidl-frontend/src/lib/licenceSvg.ts): change both together.

On Basic and Enterprise with a logo uploaded (Organization.logo_data) the
company name is a large, faint watermark behind the details, so the licence
can't be passed off as anyone else's. Trial licences carry no watermark.

Served at GET /api/licenses/<number>.png?s=<signature>  (licence_views.py)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import math
from datetime import datetime

import requests
from django.conf import settings
from PIL import Image, ImageChops, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

S = 2  # draw at 2x for a crisp image
CARD_W, CARD_H, SHADOW = 800, 470, 13
W, H = CARD_W + SHADOW, CARD_H + SHADOW
INK, YELLOW, CREAM = "#14140F", "#FFCC00", "#F5ECD2"
NAVY, PALE, PHOTO_BG, SILHOUETTE = "#16233A", "#E8EEF7", "#E9E2C6", "#8A8468"


def _mix(color: str, over: str, amount: float) -> tuple:
    """`color` at `amount` opacity over `over`, as a solid colour (Pillow
    replaces pixels instead of blending when drawing on RGBA)."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(over[i:i + 2], 16) for i in (1, 3, 5)]
    return tuple(round(x * amount + y * (1 - amount)) for x, y in zip(a, b)) + (255,)


MUTED = _mix(INK, YELLOW, 0.62)


def signature(number: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"licence:{number}".encode(), hashlib.sha256).hexdigest()[:20]


def image_url(number: str, *, download: bool = False, version: str = "") -> str:
    """`version` changes with the avatar so Slack fetches the new image."""
    url = f"{settings.BACKEND_URL}/api/licenses/{number}.png?s={signature(number)}"
    return url + (f"&v={version}" if version else "") + ("&download=1" if download else "")


LICENCE_DESIGN = "7"  # bump when the licence's look changes (new image links → Slack refetches)


def avatar_version(member) -> str:
    return hashlib.sha256((member.avatar_data or "").encode()).hexdigest()[:8] if member.avatar_data else ""


def url_version(member, org=None) -> str:
    """Everything that changes the licence picture (for its link and cache)."""
    from .card_image import logo_version

    parts = [LICENCE_DESIGN, avatar_version(member) or "noavatar",
             logo_version(org) if org is not None else "aidl",
             hashlib.sha256(f"{member.full_name}|{org.name if org else ''}|{member.licence_issued_at}|"
                            f"{member.licence_expires_at}".encode()).hexdigest()[:6]]
    return "-".join(parts)


def png_for(member, org) -> bytes:
    """The holder's licence PNG — drawn once, then served from the cache."""
    from .image_cache import png

    return png(f"lic:{member.licence_number}:{url_version(member, org)}", lambda: render(
        name=member.full_name or member.email, org_name=org.name if org else "", number=member.licence_number,
        issued=member.licence_issued_at, expires=member.licence_expires_at, org=org,
        avatar_data=member.avatar_data or ""))


def _font(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    return _lato(weight, size * S)


def _lato(weight: str, px: int) -> ImageFont.FreeTypeFont:
    from pathlib import Path

    return ImageFont.truetype(str(Path(__file__).resolve().parent / "data" / "fonts" / f"Lato-{weight}.ttf"), px)


def text_watermark(img: Image.Image, text: str, *, angle: float = 0, color: str = "#14140F",
                   opacity: float = 0.08, width_ratio: float = 0.8, spacing: float = 0.12, font=None) -> None:
    """A large, faint company-name watermark across the middle of `img`
    (rotated by `angle` degrees — slanted on the Highway Code / AUP cards)."""
    text = (text or "").upper().strip()
    if not text:
        return
    font = font or _lato("Black", 200)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    gap = 200 * spacing
    widths = [probe.textlength(ch, font=font) for ch in text]
    layer = Image.new("L", (int(sum(widths) + gap * len(text)) + 40, 260), 0)
    ld = ImageDraw.Draw(layer)
    x = 20
    for ch, w in zip(text, widths):
        ld.text((x, 130), ch, font=font, fill=255, anchor="lm")
        x += w + gap
    layer = layer.crop(layer.getbbox() or (0, 0, layer.width, layer.height))
    if angle:
        layer = layer.rotate(angle, expand=True, resample=Image.BICUBIC)
    scale = min(img.width * width_ratio / layer.width, img.height * 0.85 / layer.height)
    layer = layer.resize((max(1, int(layer.width * scale)), max(1, int(layer.height * scale))), Image.LANCZOS)
    tint = Image.new("RGBA", layer.size, color)
    tint.putalpha(layer.point(lambda a: int(a * opacity)))
    img.alpha_composite(tint, ((img.width - layer.width) // 2, (img.height - layer.height) // 2))


def _aidl_logo() -> Image.Image | None:
    from pathlib import Path

    try:
        return Image.open(Path(__file__).resolve().parent / "static" / "aidl" / "logo-transparent.png").convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def _xy(*v):
    return tuple(int(x * S) for x in v)


def _logo(org) -> Image.Image | None:
    if org is None or not getattr(org, "logo_data", ""):
        return None
    try:
        return Image.open(io.BytesIO(base64.b64decode(org.logo_data))).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def brand_logo(org) -> Image.Image | None:
    """The company logo on licences and cards — Basic and Enterprise only
    (Trial shows the AIDL mark)."""
    if org is None or getattr(org, "plan", "trial") not in ("basic", "enterprise"):
        return None
    return _logo(org)


def _avatar(url: str = "", data: str = "") -> Image.Image | None:
    if data:
        try:
            return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGBA")
        except Exception:  # noqa: BLE001
            return None
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception:  # noqa: BLE001 — the silhouette is used instead
        return None


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Cover-crop to w×h."""
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _contain(img: Image.Image, w: int, h: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((w, h), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2), img)
    return canvas


def _card_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """The licence's fonts, the closest open fonts to the website's Arial Black
    (heavy), Arial Bold (sans) and Courier New Bold (mono)."""
    from pathlib import Path

    fonts = Path(__file__).resolve().parent / "data" / "fonts"
    if kind == "heavy":
        return ImageFont.truetype(str(fonts / "ArchivoBlack-Regular.ttf"), size * S)
    if kind == "mono":
        return ImageFont.truetype(str(fonts / "CourierPrime-Bold.ttf"), size * S)
    font = ImageFont.truetype(str(fonts / "Arimo.ttf"), size * S)
    font.set_variation_by_name("Bold")
    return font


def _spaced(d, x: float, y: float, text: str, font, fill, spacing: float = 0) -> None:
    """Text from baseline (x, y), with letter-spacing as on the website."""
    px = x * S
    for ch in text:
        d.text((px, y * S), ch, font=font, fill=fill, anchor="ls")
        px += d.textlength(ch, font=font) + spacing * S


def _spaced_width(d, text: str, font, spacing: float = 0) -> float:
    return sum(d.textlength(ch, font=font) for ch in text) / S + spacing * len(text)


def _fitted(d, text: str, kind: str, size: int, width: int) -> ImageFont.FreeTypeFont:
    """The font at `size`, or smaller until `text` fits in `width`."""
    while size > 14 and _spaced_width(d, text, _card_font(kind, size)) > width:
        size -= 1
    return _card_font(kind, size)


def _photo(card: Image.Image, avatar: Image.Image | None) -> None:
    """The avatar the holder created, narrower than the frame so the panel shows
    above and below it (as designed), else a silhouette."""
    x, y, w, h = 40, 148, 150, 188
    pic = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    if avatar is not None:
        pic.alpha_composite(_contain(avatar, w * S, h * S))
    else:
        pic.paste(PHOTO_BG, (0, 0, pic.width, pic.height))
        d = ImageDraw.Draw(pic)
        d.ellipse(_xy(77 - x, 184 - y, 153 - x, 260 - y), fill=SILHOUETTE)
        d.pieslice(_xy(50 - x, 268 - y, 180 - x, 268 - y + 136), 180, 360, fill=SILHOUETTE)
    mask = Image.new("L", pic.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic.width - 1, pic.height - 1), radius=10 * S, fill=255)
    mask = ImageChops.multiply(mask, pic.getchannel("A"))
    card.paste(pic, _xy(x, y), mask)
    ImageDraw.Draw(card).rounded_rectangle(_xy(x - 2, y - 2, x + w + 2, y + h + 2), radius=12 * S, outline=PALE, width=4 * S)


def _texture(card: Image.Image) -> None:
    """Faint rings across the panel: 18px apart, the grid turned 30 degrees."""
    layer = Image.new("L", card.size, 0)
    d = ImageDraw.Draw(layer)
    cos, sin = math.cos(math.radians(30)), math.sin(math.radians(30))
    for i in range(-40, 80):
        for j in range(-40, 80):
            px, py = 9 + 18 * i, 9 + 18 * j
            cx, cy = px * cos - py * sin, px * sin + py * cos
            if -9 < cx < CARD_W + 9 and 100 < cy < 420:
                d.ellipse(_xy(cx - 7, cy - 7, cx + 7, cy + 7), outline=15, width=S)
    tint = Image.new("RGBA", card.size, PALE)
    tint.putalpha(layer)
    card.alpha_composite(tint)


def render(*, name: str, org_name: str, number: str, issued: datetime | None, expires: datetime | None = None,
           org=None, avatar_url: str = "", avatar_data: str = "") -> bytes:
    card = Image.new("RGBA", _xy(CARD_W, CARD_H), NAVY)
    _texture(card)
    d = ImageDraw.Draw(card)

    # header band, hazard stripe
    d.rectangle(_xy(0, 0, CARD_W, 112), fill=INK)
    period = 22 * math.sqrt(2)
    for k in range(0, 40):
        c = k * period
        d.polygon([_xy(c, 124), _xy(c + period / 2, 124), _xy(c + period / 2 + 12, 112),
                   _xy(c + 12, 112)], fill=INK)
    d.ellipse(_xy(24, 16, 104, 96), fill=YELLOW)
    badge_src = _aidl_logo()
    if badge_src is not None:
        size = 72 * S
        badge = badge_src.resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        card.paste(badge, _xy(28, 20), ImageChops.multiply(mask, badge.getchannel("A")))
    _spaced(d, 122, 54, "AI DRIVING LICENCE", _card_font("heavy", 32), YELLOW, 1)
    who = f"ISSUED FOR {org_name.upper()}" if org_name else "ISSUED UNDER THE AIDL PUBLIC REGISTRY"
    who = who if len(who) <= 44 else who[:43].rstrip() + "…"
    _spaced(d, 124, 84, who, _card_font("sans", 15), CREAM, 2)

    if brand_logo(org) is not None:  # company name, large and faint, behind the details
        body = card.crop(_xy(220, 126, CARD_W, 408))
        text_watermark(body, org_name, opacity=0.07, width_ratio=0.9, color=PALE)
        card.paste(body, _xy(220, 126))

    _photo(card, _avatar(avatar_url, avatar_data))

    label_ink = _mix(PALE, NAVY, 0.62)

    def label(x, y, text):
        _spaced(d, x, y, text, _card_font("sans", 13), label_ink, 1.2)

    label(220, 168, "1. NAME")
    shown = name.upper()
    _spaced(d, 220, 208, shown, _fitted(d, shown, "heavy", 38, CARD_W - 220 - 24), PALE)
    label(220, 252, "2. ISSUED")
    _spaced(d, 220, 280, issued.strftime("%d/%m/%Y") if issued else "—", _card_font("sans", 22), PALE)
    label(380, 252, "3. VALID UNTIL")
    _spaced(d, 380, 280, expires.strftime("%d/%m/%Y") if expires else "—", _card_font("sans", 22), PALE)
    label(220, 326, "4. LICENCE NO.")
    _spaced(d, 220, 356, number, _card_font("mono", 26), YELLOW, 1)

    # road footer: centre line
    d.rectangle(_xy(0, 410, CARD_W, CARD_H), fill=INK)
    for x in range(0, CARD_W, 42):
        d.rectangle(_xy(x, 438.5, x + 26, 441.5), fill=YELLOW)

    # rounded card edge + hard shadow
    edge = (0, 0, CARD_W * S - 1, CARD_H * S - 1)
    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(edge, radius=27 * S, fill=255)
    out = Image.new("RGBA", _xy(W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(out)
    draw.rounded_rectangle(_xy(SHADOW, SHADOW, W, H), radius=27 * S, fill=INK)
    out.paste(card, (0, 0), mask)
    draw.rounded_rectangle(edge, radius=27 * S, outline=INK, width=6 * S)
    buf = io.BytesIO()
    out.save(buf, "PNG", compress_level=6)  # optimize=True takes seconds on the server
    return buf.getvalue()


def render_void(number: str) -> bytes:
    """For a licence number that was revoked or reset."""
    img = Image.new("RGBA", _xy(W, 220), "#E9E2C6")
    d = ImageDraw.Draw(img)
    d.rectangle(_xy(0, 0, W, 64), fill=INK)
    d.text(_xy(28, 42), "AI DRIVING LICENCE", font=_font(24, "Black"), fill=YELLOW, anchor="ls")
    d.text(_xy(28, 118), "This licence is no longer valid", font=_font(26, "Black"), fill=INK, anchor="ls")
    d.text(_xy(28, 156), f"{number} was withdrawn or reset. Check your latest AIDL messages for your",
           font=_font(15, "Regular"), fill=MUTED, anchor="ls")
    d.text(_xy(28, 180), "current licence.", font=_font(15, "Regular"), fill=MUTED, anchor="ls")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def save_logo(raw: bytes) -> str:
    """Normalise an uploaded logo to a ≤512px PNG; returns base64."""
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    img.thumbnail((512, 512), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()
