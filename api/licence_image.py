"""The AI Driving License as a PNG (the design in E:\\slack-userflow,
"Trial Flow C"), drawn with Pillow so Slack can show it in an image block.

The organization's logo (Organization.logo_data) sits in the header circle
and as a large, faint watermark — the licence carries the company's mark,
so it can't be passed off as anyone else's.

Served at GET /api/licenses/<number>.png?s=<signature>  (licence_views.py)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
from datetime import datetime

import requests
from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

S = 2  # draw at 2x for a crisp image
W, H = 800, 470
INK, YELLOW, CREAM, GREEN, RED = "#14140F", "#FFCC00", "#F5ECD2", "#1A7F37", "#C62F24"


def _mix(color: str, over: str, amount: float) -> tuple:
    """`color` at `amount` opacity over `over`, as a solid colour (Pillow
    replaces pixels instead of blending when drawing on RGBA)."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(over[i:i + 2], 16) for i in (1, 3, 5)]
    return tuple(round(x * amount + y * (1 - amount)) for x, y in zip(a, b)) + (255,)


TEXTURE = _mix(INK, YELLOW, 0.07)
MUTED = _mix(INK, YELLOW, 0.62)
LINE = _mix(INK, YELLOW, 0.5)


def signature(number: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"licence:{number}".encode(), hashlib.sha256).hexdigest()[:20]


def image_url(number: str, *, download: bool = False) -> str:
    url = f"{settings.BACKEND_URL}/api/licenses/{number}.png?s={signature(number)}"
    return url + ("&download=1" if download else "")


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size * S)


def _xy(*v):
    return tuple(int(x * S) for x in v)


def _logo(org) -> Image.Image | None:
    if org is None or not getattr(org, "logo_data", ""):
        return None
    try:
        return Image.open(io.BytesIO(base64.b64decode(org.logo_data))).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def _avatar(url: str) -> Image.Image | None:
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


def _circle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size - 1, size - 1), fill=255)
    return m


def render(*, name: str, org_name: str, number: str, issued: datetime | None, org=None,
           avatar_url: str = "") -> bytes:
    img = Image.new("RGBA", _xy(W, H), YELLOW)
    d = ImageDraw.Draw(img)

    # faint guilloche texture
    for y in range(0, H, 18):
        for x in range(0 if (y // 18) % 2 == 0 else 9, W, 18):
            d.ellipse(_xy(x - 7, y - 7, x + 7, y + 7), outline=TEXTURE, width=S)

    logo = _logo(org)
    if logo is not None:  # large faint watermark, bottom right
        size = 290 * S
        wm = _contain(logo, size, size)
        alpha = wm.getchannel("A").point(lambda a: int(a * 0.10))
        wm.putalpha(alpha)
        mask = _circle_mask(size)
        wm.putalpha(Image.composite(wm.getchannel("A"), Image.new("L", wm.size, 0), mask))
        img.alpha_composite(wm, _xy(515, 118))

    # header band + hazard stripe
    d.rectangle(_xy(0, 0, W, 112), fill=INK)
    for x in range(-30, W + 30, 22):
        d.polygon([_xy(x, 124), _xy(x + 11, 124), _xy(x + 23, 112), _xy(x + 12, 112)], fill=INK)
    d.ellipse(_xy(24, 16, 104, 96), fill=YELLOW)
    if logo is not None:
        size = 72 * S
        badge = _contain(logo, size, size)
        clip = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        clip.paste(badge, (0, 0), _circle_mask(size))
        img.alpha_composite(clip, _xy(28, 20))
    else:
        d.text(_xy(64, 56), "AI", font=_font(24), fill=INK, anchor="mm")
    d.text(_xy(122, 54), "AI DRIVING LICENSE", font=_font(32), fill=YELLOW, anchor="ls")
    d.text(_xy(124, 84), f"ISSUED FOR {org_name.upper()}"[:46], font=_font(15), fill=CREAM, anchor="ls")

    # photo (Slack profile picture, else a silhouette) + signature
    box = (40, 148, 190, 336)
    avatar = _avatar(avatar_url)
    if avatar is not None:
        ph = _fit(avatar, 150 * S, 188 * S)
        m = Image.new("L", ph.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, ph.width - 1, ph.height - 1), radius=10 * S, fill=255)
        img.paste(ph, _xy(40, 148), m)
    else:
        d.rounded_rectangle(_xy(*box), radius=10 * S, fill="#E9E2C6")
        d.ellipse(_xy(77, 184, 153, 260), fill="#8A8468")
        d.pieslice(_xy(50, 268, 180, 404), 180, 360, fill="#8A8468")
    d.rounded_rectangle(_xy(*box), radius=10 * S, outline=INK, width=4 * S)
    d.text(_xy(44, 372), name.title()[:22], font=_font(20), fill=INK, anchor="ls")
    d.rectangle(_xy(40, 380, 190, 381), fill=LINE)
    d.text(_xy(40, 396), "HOLDER'S SIGNATURE", font=_font(11), fill=MUTED, anchor="ls")

    def label(x, y, t):
        d.text(_xy(x, y), t, font=_font(13), fill=MUTED, anchor="ls")

    label(220, 168, "1. NAME")
    d.text(_xy(220, 208), name.upper()[:24], font=_font(34), fill=INK, anchor="ls")
    label(220, 252, "2. ISSUED")
    d.text(_xy(220, 280), issued.strftime("%d/%m/%Y") if issued else "—", font=_font(22), fill=INK, anchor="ls")
    label(450, 252, "3. STATUS")
    d.rounded_rectangle(_xy(450, 260, 560, 290), radius=15 * S, fill=GREEN)
    d.ellipse(_xy(463, 270, 473, 280), fill="#FFFFFF")
    d.text(_xy(480, 281), "ACTIVE", font=_font(16), fill="#FFFFFF", anchor="ls")
    label(220, 326, "4. LICENSE NO.")
    d.text(_xy(220, 356), number, font=_font(26), fill=RED, anchor="ls")

    # road footer: centre line + machine-readable line
    d.rectangle(_xy(0, 410, W, H), fill=INK)
    for x in range(0, W, 42):
        d.rectangle(_xy(x, 419, x + 26, 421), fill=YELLOW)
    surname_first = "<<".join(reversed(name.upper().split()))
    mrz = ("AIDL<" + number.replace("AIDL-", "").replace("-", "<") + "<<" + surname_first + "<" * 44)[:44]
    d.text(_xy(40, 452), mrz, font=_font(17), fill=CREAM, anchor="ls")

    # rounded card edge
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((3 * S, 3 * S, img.width - 3 * S, img.height - 3 * S), radius=24 * S, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    ImageDraw.Draw(out).rounded_rectangle((3 * S, 3 * S, img.width - 3 * S, img.height - 3 * S), radius=24 * S,
                                          outline=INK, width=6 * S)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def save_logo(raw: bytes) -> str:
    """Normalise an uploaded logo to a ≤512px PNG; returns base64."""
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    img.thumbnail((512, 512), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()
