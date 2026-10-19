"""The AI Driving License as a PNG (the design in E:\\slack-userflow,
"Trial Flow C"), drawn with Pillow so Slack can show it in an image block.

On Basic and Enterprise the organization's logo (Organization.logo_data) sits
in the header circle and the company name is a large, faint watermark — the
licence carries the company's mark, so it can't be passed off as anyone
else's. Trial licences carry the AIDL logo and no watermark.

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


def image_url(number: str, *, download: bool = False, version: str = "") -> str:
    """`version` changes with the avatar so Slack fetches the new image."""
    url = f"{settings.BACKEND_URL}/api/licenses/{number}.png?s={signature(number)}"
    return url + (f"&v={version}" if version else "") + ("&download=1" if download else "")


LICENCE_DESIGN = "3"  # bump when the licence's look changes (new image links → Slack refetches)


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
                   opacity: float = 0.08, width_ratio: float = 0.8, spacing: float = 0.12) -> None:
    """A large, faint company-name watermark across the middle of `img`
    (rotated by `angle` degrees — slanted on the Highway Code / AUP cards)."""
    text = (text or "").upper().strip()
    if not text:
        return
    font = _lato("Black", 200)
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
        return Image.open(Path(__file__).resolve().parent / "static" / "aidl" / "logo.png").convert("RGBA")
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


def _circle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size - 1, size - 1), fill=255)
    return m


def render(*, name: str, org_name: str, number: str, issued: datetime | None, expires: datetime | None = None,
           org=None, avatar_url: str = "", avatar_data: str = "") -> bytes:
    img = Image.new("RGBA", _xy(W, H), YELLOW)
    d = ImageDraw.Draw(img)

    # faint guilloche texture
    for y in range(0, H, 18):
        for x in range(0 if (y // 18) % 2 == 0 else 9, W, 18):
            d.ellipse(_xy(x - 7, y - 7, x + 7, y + 7), outline=TEXTURE, width=S)

    logo = brand_logo(org)

    # header band + hazard stripe
    d.rectangle(_xy(0, 0, W, 112), fill=INK)
    for x in range(-30, W + 30, 22):
        d.polygon([_xy(x, 124), _xy(x + 11, 124), _xy(x + 23, 112), _xy(x + 12, 112)], fill=INK)
    d.ellipse(_xy(24, 16, 104, 96), fill=YELLOW)
    badge_src = _aidl_logo()  # as designed: AIDL in the circle, the company as the watermark
    if badge_src is not None:
        size = 72 * S
        badge = _contain(badge_src, size, size)
        clip = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        clip.paste(badge, (0, 0), _circle_mask(size))
        img.alpha_composite(clip, _xy(28, 20))
    d.text(_xy(122, 56), "AI DRIVING LICENSE", font=_font(34, "Black"), fill=YELLOW, anchor="ls")
    spaced = " ".join(f"ISSUED FOR {org_name.upper()}"[:40])
    d.text(_xy(124, 84), spaced, font=_font(12, "Black"), fill=CREAM, anchor="ls")

    if logo is not None:  # company name, large and faint, behind the details
        body = img.crop(_xy(200, 132, W, 404))
        text_watermark(body, org_name, opacity=0.10, width_ratio=0.9)
        img.paste(body, _xy(200, 132))

    # photo (the avatar the holder created, else a silhouette) + signature
    box = (40, 148, 190, 336)
    avatar = _avatar(avatar_url, avatar_data)
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
    d.text(_xy(115, 368), name.upper()[:20], font=_font(17, "BlackItalic"), fill=INK, anchor="ms")
    d.rectangle(_xy(40, 378, 190, 379), fill=LINE)
    d.text(_xy(115, 394), "HOLDER'S SIGNATURE", font=_font(10, "Bold"), fill=MUTED, anchor="ms")

    def label(x, y, t):
        d.text(_xy(x, y), t, font=_font(11, "Bold"), fill=MUTED, anchor="ls")

    label(220, 168, "1. NAME")
    d.text(_xy(220, 210), name.upper()[:22], font=_font(36, "Black"), fill=INK, anchor="ls")
    label(220, 252, "2. ISSUED")
    d.text(_xy(220, 280), issued.strftime("%d/%m/%Y") if issued else "—", font=_font(22, "Black"), fill=INK,
           anchor="ls")
    label(385, 252, "3. VALID UNTIL")
    d.text(_xy(385, 280), expires.strftime("%d/%m/%Y") if expires else "—", font=_font(22, "Black"), fill=INK,
           anchor="ls")
    expired = expires is not None and expires < datetime.now(expires.tzinfo)
    label(560, 252, "4. STATUS")
    pill, word = (RED, "EXPIRED") if expired else (GREEN, "ACTIVE")
    d.rounded_rectangle(_xy(560, 262, 668, 290), radius=14 * S, fill=pill)
    d.ellipse(_xy(572, 272, 580, 280), fill="#FFFFFF")
    d.text(_xy(588, 282), word, font=_font(15, "Black"), fill="#FFFFFF", anchor="ls")
    label(220, 326, "5. LICENSE NO.")
    d.text(_xy(220, 358), number, font=_font(26, "Bold"), fill=RED, anchor="ls")

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


def render_void(number: str) -> bytes:
    """For a licence number that was revoked or reset."""
    img = Image.new("RGBA", _xy(W, 220), "#E9E2C6")
    d = ImageDraw.Draw(img)
    d.rectangle(_xy(0, 0, W, 64), fill=INK)
    d.text(_xy(28, 42), "AI DRIVING LICENSE", font=_font(24, "Black"), fill=YELLOW, anchor="ls")
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
