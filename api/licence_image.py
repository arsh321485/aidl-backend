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
from datetime import datetime

import requests
from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

S = 2  # draw at 2x for a crisp image
W, H = 800, 468
CARD_W, CARD_H = 787, 456  # the rest of W, H is the hard shadow
INK, YELLOW, CREAM, GREEN, RED = "#14140F", "#FFCC00", "#F5ECD2", "#1A7F37", "#C62F24"
NAVY, PALE, LABEL, PHOTO_BG, SILHOUETTE, AMBER = "#16233A", "#E8EEF7", "#8E97A8", "#D1D8E2", "#7E8898", "#FFB000"


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


LICENCE_DESIGN = "5"  # bump when the licence's look changes (new image links → Slack refetches)


def avatar_version(member) -> str:
    return hashlib.sha256((member.avatar_data or "").encode()).hexdigest()[:8] if member.avatar_data else ""


def status_of(member) -> str:
    """What the licence says today: it changes without the licence being edited."""
    if getattr(member, "slack_left_at", None):
        return "SUSPENDED"
    expires = member.licence_expires_at
    return "EXPIRED" if expires is not None and expires < datetime.now(expires.tzinfo) else "ACTIVE"


def url_version(member, org=None) -> str:
    """Everything that changes the licence picture (for its link and cache)."""
    from .card_image import logo_version

    parts = [LICENCE_DESIGN, status_of(member).lower(), avatar_version(member) or "noavatar",
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
        avatar_data=member.avatar_data or "", status=status_of(member)))


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


def _mono(size: int) -> ImageFont.FreeTypeFont:
    from pathlib import Path

    return ImageFont.truetype(str(Path(__file__).resolve().parent / "data" / "fonts" / "JetBrainsMono-Bold.ttf"), size * S)


def _fitted(d, text: str, weight: str, size: int, width: int) -> ImageFont.FreeTypeFont:
    """The font at `size`, or smaller until `text` fits in `width`."""
    while size > 14 and d.textlength(text, font=_font(size, weight)) > width * S:
        size -= 1
    return _font(size, weight)


def _photo(card: Image.Image, avatar: Image.Image | None, x: int, y: int, w: int, h: int) -> None:
    """The avatar the holder created (else a silhouette) in a pale frame."""
    pic = Image.new("RGBA", (w * S, h * S), PHOTO_BG)
    if avatar is not None:
        pic.alpha_composite(_fit(avatar, w * S, h * S))
    else:
        d = ImageDraw.Draw(pic)
        cx = w * S // 2
        d.ellipse((cx - 34 * S, 34 * S, cx + 34 * S, 102 * S), fill=SILHOUETTE)
        d.pieslice((cx - 62 * S, 112 * S, cx + 62 * S, (h + 62) * S), 180, 360, fill=SILHOUETTE)
    mask = Image.new("L", pic.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic.width - 1, pic.height - 1), radius=9 * S, fill=255)
    card.paste(pic, (x * S, y * S), mask)
    ImageDraw.Draw(card).rounded_rectangle((x * S, y * S, (x + w) * S, (y + h) * S), radius=9 * S, outline=PALE,
                                           width=4 * S)


def render(*, name: str, org_name: str, number: str, issued: datetime | None, expires: datetime | None = None,
           org=None, avatar_url: str = "", avatar_data: str = "", status: str = "") -> bytes:
    if not status:
        status = "EXPIRED" if expires is not None and expires < datetime.now(expires.tzinfo) else "ACTIVE"
    card = Image.new("RGBA", (CARD_W * S, CARD_H * S), INK)
    d = ImageDraw.Draw(card)

    # header: AIDL logo, title, who issued it
    badge_src = _aidl_logo()
    if badge_src is not None:
        badge = _contain(badge_src.crop(badge_src.getbbox()), 84 * S, 84 * S)
        card.alpha_composite(badge, _xy(20, 13))
    d.text(_xy(121, 56), "AI DRIVING LICENCE", font=_font(36, "Black"), fill=YELLOW, anchor="ls")
    who = f"ISSUED FOR {org_name.upper()}" if org_name else "ISSUED UNDER THE AIDL PUBLIC REGISTRY"
    who = who if len(who) <= 44 else who[:43].rstrip() + "…"
    d.text(_xy(122, 85), " ".join(who), font=_font(12, "Black"), fill=CREAM, anchor="ls")

    # hazard stripe, navy panel
    d.rectangle(_xy(8, 110, 778, 123), fill=INK)
    for x in range(8, 778, 19):
        d.polygon([_xy(x, 123), _xy(min(x + 10, 778), 123), _xy(min(x + 20, 778), 110), _xy(min(x + 10, 778), 110)], fill=NAVY)
    d.rectangle(_xy(8, 123, 778, 403), fill=NAVY)
    if brand_logo(org) is not None:  # company name, large and faint, behind the details
        body = card.crop(_xy(200, 124, 778, 402))
        text_watermark(body, org_name, opacity=0.07, width_ratio=0.9, color=PALE)
        card.paste(body, _xy(200, 124))

    # road: centre line
    for x in range(8, 770, 39):
        d.rectangle(_xy(x, 431, x + 23, 433), fill=YELLOW)

    _photo(card, _avatar(avatar_url, avatar_data), 38, 144, 151, 189)

    def label(x, y, text):
        d.text(_xy(x, y), text, font=_font(11, "Black"), fill=LABEL, anchor="ls")

    x = 217
    label(x, 164, "1. NAME")
    shown = name.upper()
    d.text(_xy(x, 204), shown, font=_fitted(d, shown, "Black", 38, 778 - x - 24), fill=PALE, anchor="ls")
    label(x, 248, "2. ISSUED")
    d.text(_xy(x, 276), issued.strftime("%d/%m/%Y") if issued else "—", font=_font(22, "Bold"), fill=PALE, anchor="ls")
    label(372, 248, "3. VALID UNTIL")
    d.text(_xy(372, 276), expires.strftime("%d/%m/%Y") if expires else "—", font=_font(22, "Bold"), fill=PALE, anchor="ls")
    label(x, 321, "4. LICENCE NO.")
    d.text(_xy(x, 352), number, font=_mono(25), fill=YELLOW, anchor="ls")
    label(540, 321, "5. STATUS")
    pill, fg = {"ACTIVE": (GREEN, "#FFFFFF"), "EXPIRED": (RED, "#FFFFFF")}.get(status, (AMBER, INK))
    font = _font(14, "Black")
    width = d.textlength(status, font=font) / S + 42
    d.rounded_rectangle(_xy(540, 331, 540 + width, 359), radius=14 * S, fill=pill)
    d.ellipse(_xy(552, 341, 560, 349), fill=fg)
    d.text(_xy(568, 350), status, font=font, fill=fg, anchor="ls")

    # rounded corners + hard shadow
    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, card.width - 1, card.height - 1), radius=26 * S, fill=255)
    out = Image.new("RGBA", _xy(W, H), (0, 0, 0, 0))
    ImageDraw.Draw(out).rounded_rectangle(_xy(11, 10, 798, 466), radius=26 * S, fill=INK)
    out.paste(card, (0, 0), mask)
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
