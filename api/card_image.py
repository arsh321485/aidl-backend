"""The Highway Code and Traffic Light Check cards as PNGs (design: E:\\image.png),
drawn with Pillow so Slack can show them in image blocks.

On Basic and Enterprise the organization's logo (Organization.logo_data)
is the badge in the card's brand line and a faint watermark across the card;
Trial cards carry the AIDL logo and no watermark (licence_image.brand_logo).

Served at GET /api/cards/<kind>/<org id>.png?s=<signature>&v=<logo version>
(licence_views.card_png). `v` changes with the logo, so Slack fetches the
new image after the logo changes.
"""

from __future__ import annotations

import hashlib
import hmac
import io
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

from .licence_image import _circle_mask, _contain, brand_logo

KINDS = ("highway_code", "traffic_light")
S = 2  # draw at 2x for a crisp image
W, PAD = 720, 30
COL = PAD + 58  # content column, right of the brand badge
TEXT_X = COL + 66  # rule text, right of the signs
INK, GREY, RULE = "#1D1C1D", "#616061", "#DDDDDD"
_DIR = Path(__file__).resolve().parent
_FONTS = _DIR / "data" / "fonts"
_AIDL_LOGO = _DIR / "static" / "aidl" / "logo.png"

HIGHWAY = {
    "title": "The Highway Code",
    "intro": "The everyday rules for using AI safely and confidently. Learn them, follow them, drive happy.",
    "rules": [
        ("stop", "Keep Private Things Private",
         "Never paste **passwords, ID numbers, bank details, or your home address** into a consumer AI tool. "
         "Once it's in, you've lost control of it."),
        ("check", "Check Before You Trust",
         "AI can state wrong things confidently. **Verify facts, dates, and numbers** against a real source "
         "before you rely on them."),
        ("you", "You're Still the Driver",
         "AI drafts; you decide. **Read and edit every output** and make it your own before you use or send it."),
        ("ask", "Better Prompt, Better Answer",
         "Vague questions get vague answers. **Say who the AI should be, what you want, and how it should "
         "look** — that's the PREP habit."),
        ("oneway", "Mind What You Share",
         "Free tools may learn from what you type. **Treat every prompt like a postcard** — assume it could be "
         "read."),
    ],
}

TRAFFIC = {
    "title": "Traffic Light Check",
    "intro": "Before you paste anything into an AI, check the lights.",
    "lights": [
        ("green", "GREEN · GO", "Public, non-personal.",
         ["General questions & explanations", "Public articles to summarise", "Story, recipe, and idea prompts"],
         "→ Any tool you like — then check facts."),
        ("amber", "AMBER · CAUTION", "A little personal.",
         ["Your first name or city", "Your rough plans or preferences", "Non-sensitive everyday details"],
         "→ Use a placeholder or remove it first."),
        ("red", "RED · STOP", "Private, keep it out.",
         ["Passwords, PINs, verification codes", "Bank/card numbers, national ID",
          "Home address, other people's data"],
         "→ Never paste. Anonymise, then retry."),
    ],
}

LIGHT = {  # tint, bar, pill, lit lamp
    "green": ("#EEF7F0", "#2E9E4F", "#1E7F3B", "#3DD16A"),
    "amber": ("#FDF6E8", "#F0B400", "#A36A00", "#F7C21B"),
    "red": ("#FCEEEC", "#D9443A", "#C2352B", "#F0453A"),
}


# ---------- links ----------

def logo_version(org) -> str:
    logo = brand_logo(org)
    return hashlib.sha256((org.logo_data or "").encode()).hexdigest()[:8] if logo is not None else "aidl"


def signature(kind: str, org_id: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"card:{kind}:{org_id}".encode(), hashlib.sha256).hexdigest()[:20]


def image_url(kind: str, org) -> str:
    oid = str(org.pk)
    return f"{settings.BACKEND_URL}/api/cards/{kind}/{oid}.png?s={signature(kind, oid)}&v={logo_version(org)}"


# ---------- drawing helpers ----------

@lru_cache(maxsize=16)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONTS / f"Lato-{weight}.ttf"), size * S)


def _xy(*v):
    return tuple(int(round(x * S)) for x in v)


def _runs(text: str) -> list[tuple[str, bool]]:
    """'plain **bold** plain' → words tagged bold or not."""
    words = []
    for i, part in enumerate(text.split("**")):
        words += [(w, i % 2 == 1) for w in part.split()]
    return words


def _rich(d: ImageDraw.ImageDraw, x: float, y: float, width: float, text: str, size: int = 16,
          line: float = 24, color: str = INK) -> float:
    """Wrap `text` (with **bold** runs) into `width`; returns the y below it."""
    regular, bold = _font("Regular", size), _font("Bold", size)
    space = d.textlength(" ", font=regular) / S
    cx, lines = 0.0, [[]]
    for word, is_bold in _runs(text):
        w = d.textlength(word, font=bold if is_bold else regular) / S
        if cx and cx + w > width:
            lines.append([])
            cx = 0.0
        lines[-1].append((cx, word, is_bold))
        cx += w + space
    for row in lines:
        for wx, word, is_bold in row:
            d.text(_xy(x + wx, y), word, font=bold if is_bold else regular, fill=color, anchor="la")
        y += line
    return y


def _spaced(d, x, y, text, font, fill, spacing=1.6):
    for ch in text:
        d.text(_xy(x, y), ch, font=font, fill=fill, anchor="ls")
        x += d.textlength(ch, font=font) / S + spacing
    return x


def _dashes(d, y):
    for x in range(COL, W - PAD, 7):
        d.line([_xy(x, y), _xy(min(x + 3, W - PAD), y)], fill=RULE, width=S)


def _sign(d: ImageDraw.ImageDraw, kind: str, cx: float, cy: float) -> None:
    small = _font("Black", 11)
    if kind == "stop":
        import math
        r = 25
        pts = [_xy(cx + r * math.cos(math.radians(22.5 + 45 * i)), cy + r * math.sin(math.radians(22.5 + 45 * i)))
               for i in range(8)]
        d.polygon(pts, fill="#E03A2F", outline="#FFFFFF")
        d.text(_xy(cx, cy), "STOP", font=small, fill="#FFFFFF", anchor="mm")
    elif kind == "check":
        r = 27
        pts = [_xy(cx, cy - r), _xy(cx + r, cy), _xy(cx, cy + r), _xy(cx - r, cy)]
        d.polygon(pts, fill="#F5C400")
        d.line(pts + [pts[0]], fill=INK, width=2 * S, joint="curve")
        d.text(_xy(cx, cy), "CHECK", font=_font("Black", 10), fill=INK, anchor="mm")
    elif kind == "you":
        d.polygon([_xy(cx, cy - 25), _xy(cx + 25, cy + 21), _xy(cx - 25, cy + 21)], fill="#F59E0B")
        d.text(_xy(cx, cy + 12), "YOU", font=small, fill="#FFFFFF", anchor="mm")
    elif kind == "ask":
        d.ellipse(_xy(cx - 23, cy - 23, cx + 23, cy + 23), fill="#6FB3E0", outline=INK, width=2 * S)
        d.text(_xy(cx, cy), "ASK", font=small, fill="#FFFFFF", anchor="mm")
    elif kind == "oneway":
        d.rectangle(_xy(cx - 23, cy - 23, cx + 23, cy + 23), fill="#14140F")
        d.text(_xy(cx, cy - 6), "ONE", font=small, fill="#FFFFFF", anchor="mm")
        d.text(_xy(cx, cy + 7), "WAY →", font=_font("Black", 10), fill="#FFFFFF", anchor="mm")


def _traffic_icon(d, x, y, lit: str) -> None:
    """A small traffic light, the `lit` lamp on (x, y = top-left)."""
    d.rounded_rectangle(_xy(x, y, x + 15, y + 34), radius=4 * S, fill="#1D1C1D")
    for i, name in enumerate(("red", "amber", "green")):
        on = name == lit
        d.ellipse(_xy(x + 3.5, y + 3 + i * 10, x + 11.5, y + 11 + i * 10), fill=LIGHT[name][3] if on else "#4A4A4A")


def _book_icon(d, x, y) -> None:
    d.polygon([_xy(x, y + 3), _xy(x + 11, y + 1), _xy(x + 11, y + 19), _xy(x, y + 21)], fill="#E9E2C6", outline=INK)
    d.polygon([_xy(x + 11, y + 1), _xy(x + 22, y + 3), _xy(x + 22, y + 21), _xy(x + 11, y + 19)], fill="#FFFFFF",
              outline=INK)
    for k in range(3):
        d.line([_xy(x + 14, y + 7 + k * 4), _xy(x + 19, y + 8 + k * 4)], fill=GREY, width=S)


def _badge(org) -> Image.Image:
    logo = brand_logo(org)
    if logo is None:
        logo = Image.open(_AIDL_LOGO).convert("RGBA")
    size = 44 * S
    pic = _contain(logo, size, size)
    bg = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    bg.alpha_composite(pic)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(bg, (0, 0), _circle_mask(size))
    return out


def _header(img, d, org, title: str, icon) -> float:
    img.alpha_composite(_badge(org), _xy(PAD, PAD))
    x = PAD + 58
    d.text(_xy(x, PAD + 19), "AIDL", font=_font("Black", 17), fill=INK, anchor="ls")
    d.text(_xy(x + d.textlength("AIDL", font=_font("Black", 17)) / S + 8, PAD + 19), f"for {org.name}",
           font=_font("Regular", 14), fill=GREY, anchor="ls")
    icon(d, x, PAD + 28)
    d.text(_xy(x + 30, PAD + 47), title, font=_font("Black", 22), fill=INK, anchor="ls")
    return PAD + 62


# ---------- the cards ----------

def _highway(img, d, org) -> float:
    y = _header(img, d, org, HIGHWAY["title"], _book_icon)
    y = _rich(d, PAD + 58, y, W - PAD * 2 - 58, HIGHWAY["intro"], size=16, line=25) + 8
    for kind, title, body in HIGHWAY["rules"]:
        _dashes(d, y)
        top = y + 16
        d.text(_xy(TEXT_X, top), title, font=_font("Bold", 17), fill=INK, anchor="la")
        bottom = _rich(d, TEXT_X, top + 26, W - PAD - TEXT_X, body, size=15, line=22)
        _sign(d, kind, COL + 26, (top + bottom) / 2 - 4)
        y = max(bottom, top + 56) + 10
    return y


def _traffic(img, d, org) -> float:
    y = _header(img, d, org, TRAFFIC["title"], lambda dd, x, yy: _traffic_icon(dd, x + 3, yy - 3, "green"))
    y = _rich(d, PAD + 58, y, W - PAD * 2 - 58, TRAFFIC["intro"], size=16, line=25) + 10
    left, right = PAD + 58, W - PAD
    for name, title, sub, items, action in TRAFFIC["lights"]:
        tint, bar, pill, _lamp = LIGHT[name]
        top = y
        height = 52 + len(items) * 24 + 52
        d.rectangle(_xy(left, top, right, top + height), fill=tint)
        d.rectangle(_xy(left, top, left + 4, top + height), fill=bar)
        _traffic_icon(d, left + 18, top + 14, name)
        end = _spaced(d, left + 44, top + 38, title, _font("Black", 18), INK)
        d.text(_xy(end + 6, top + 38), f"— {sub}", font=_font("Regular", 14), fill=GREY, anchor="ls")
        iy = top + 56
        for item in items:
            d.ellipse(_xy(left + 26, iy + 8, left + 31, iy + 13), fill=INK)
            d.text(_xy(left + 42, iy), item, font=_font("Regular", 15), fill=INK, anchor="la")
            iy += 24
        font = _font("Black", 14)
        pw = d.textlength(action, font=font) / S + 22
        d.rounded_rectangle(_xy(left + 18, iy + 8, left + 18 + pw, iy + 36), radius=4 * S, fill=pill)
        d.text(_xy(left + 29, iy + 22), action, font=font, fill="#FFFFFF", anchor="lm")
        y = top + height + 12
    return y


def render(kind: str, org) -> bytes:
    img = Image.new("RGBA", _xy(W, 1400), "#FFFFFF")
    d = ImageDraw.Draw(img)
    bottom = (_highway if kind == "highway_code" else _traffic)(img, d, org) + PAD - 10
    img = img.crop((0, 0, img.width, int(bottom * S)))

    logo = brand_logo(org)
    if logo is not None:  # faint company watermark across the card
        size = int(min(img.width * 0.62, img.height * 0.8))
        wm = _contain(logo, size, size)
        wm.putalpha(wm.getchannel("A").point(lambda a: int(a * 0.07)))
        img.alpha_composite(wm, (img.width - size - 24 * S, (img.height - size) // 2))

    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=8 * S, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def alt_text(kind: str) -> str:
    """What screen readers (and Slack notifications) get instead of the image."""
    if kind == "highway_code":
        return "The Highway Code. " + " ".join(f"{t}: {b.replace('**', '')}" for _k, t, b in HIGHWAY["rules"])
    return "Traffic Light Check. " + " ".join(
        f"{title}, {sub} For example: {', '.join(items)}. {action.lstrip('→ ')}"
        for _n, title, sub, items, action in TRAFFIC["lights"])
