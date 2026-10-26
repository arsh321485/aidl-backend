"""The brand strip at the top of the AI policy builder popups (aup_slack.py).

Slack text can't be coloured, so the AIDL look — asphalt band, yellow
"AI POLICY", the progress road of 15 sections — is a small PNG drawn here
(Bungee + JetBrains Mono, OFL, in api/data/fonts) and kept in image_cache.

Served at GET /api/aup/banner.png?l=<label>&r=<right>&o=<org name>&at=<n>&s=<signature>
(licence_views.aup_banner_png). Slack must fetch the picture before it
shows the popup, so it is drawn before the popup is sent (`url` predraws).
"""

from __future__ import annotations

import hashlib
import hmac
import io
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

DESIGN = "2"  # bump when the strip's look changes
# Slack shows the picture at the popup's width (~480px), so it is laid out
# at that width — text sized for it reads like the rest of the popup.
S = 3  # drawn at 3x for a crisp image
W, H, PAD = 480, 104, 16
ASPHALT, YELLOW, CREAM, ROAD = "#14140F", "#FFCC00", "#F5ECD2", "#3A3A30"
_FONTS = Path(__file__).resolve().parent / "data" / "fonts"


@lru_cache(maxsize=8)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONTS / name), size * S)


def _params(label: str, right: str, org_name: str, at: int, total: int) -> dict:
    return {"l": label[:60], "r": right[:40], "o": org_name[:40], "at": str(at), "n": str(total), "v": DESIGN}


def signature(params: dict) -> str:
    msg = "|".join(f"{k}={params.get(k, '')}" for k in ("l", "r", "o", "at", "n", "v"))
    return hmac.new(settings.SECRET_KEY.encode(), f"aupbanner:{msg}".encode(), hashlib.sha256).hexdigest()[:20]


def _key(params: dict) -> str:
    return "aupbanner:" + hashlib.sha256(signature(params).encode()).hexdigest()[:24]


def url(label: str, right: str, org_name: str, at: int, total: int = 15) -> str:
    """The strip's link; the picture is drawn now so Slack gets it at once."""
    from .image_cache import predraw

    params = _params(label, right, org_name, at, total)
    predraw(png_for, params)
    return f"{settings.BACKEND_URL}/api/aup/banner.png?{urlencode({**params, 's': signature(params)})}"


def png_for(params: dict) -> bytes:
    from .image_cache import png

    return png(_key(params), lambda: render(params))


def _fit(d: ImageDraw.ImageDraw, text: str, font, width: float) -> str:
    """Shorten text with … until it fits the width."""
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def render(params: dict) -> bytes:
    """Asphalt band: AI POLICY · right text / label · ORG NAME / the road."""
    img = Image.new("RGB", (W * S, H * S), ASPHALT)
    d = ImageDraw.Draw(img)
    mono = _font("JetBrainsMono-Bold.ttf", 13)

    d.text((PAD * S, 13 * S), "AI POLICY", font=_font("Bungee-Regular.ttf", 24), fill=YELLOW)
    right = _fit(d, params.get("r", "").upper(), mono, (W - 2 * PAD - 170) * S)
    d.text(((W - PAD) * S, 20 * S), right, font=mono, fill=CREAM, anchor="ra")
    label = params.get("l", "").upper()
    d.text((PAD * S, 52 * S), label, font=mono, fill=CREAM)
    room = (W - 2 * PAD - 16) * S - d.textlength(label, font=mono)  # the org name gets what's left
    if room > 40 * S:
        d.text(((W - PAD) * S, 52 * S), _fit(d, params.get("o", "").upper(), mono, room), font=mono,
               fill=YELLOW, anchor="ra")

    total = max(int(params.get("n") or 15), 1)
    at = int(params.get("at") or 0)
    gap, top, height = 3, 80, 10
    seg = (W - 2 * PAD - gap * (total - 1)) / total
    for i in range(total):
        x0 = PAD + i * (seg + gap)
        box = (int(x0 * S), top * S, int((x0 + seg) * S), (top + height) * S)
        if i < at:
            d.rectangle(box, fill=YELLOW)
        elif i == at:
            d.rectangle(box, fill=YELLOW, outline=CREAM, width=2 * S)
        else:
            d.rectangle(box, fill=ROAD)

    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=6)
    return buf.getvalue()
