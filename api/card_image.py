"""AIDL cards as PNGs for Slack image blocks.

The Highway Code and Traffic Light Check are the designer's cards
(E:\\highway-traffic-cards.html), rendered once with headless Chrome into
api/static/cards/*.png (tools/split_card_design.py — re-run it when the
design changes). AUP points ("aup-<n>") and awareness cards
("pkg-<card no>") are drawn here with Pillow.

On Basic and Enterprise the company logo replaces the AIDL logo in the
header circle, and the Highway Code and AUP cards carry the design's
watermark: the company logo, slanted (-55°) and faint, across the card.
The Traffic Light Check has no watermark (as designed); awareness cards
keep a faint upright logo.

Trial cards carry the AIDL logo and no watermark (licence_image.brand_logo).

Served at GET /api/cards/<kind>/<org id>.png?s=<signature>&v=<version>
(licence_views.card_png). `v` changes with the logo (and the AUP version), so
Slack fetches the new image after either changes.
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

KINDS = ("highway_code", "traffic_light")  # + "aup-<n>" and "pkg-<card no>"


def valid_kind(kind: str) -> bool:
    if kind in KINDS:
        return True
    if kind.startswith("aup-"):
        return kind[4:].isdigit() and 1 <= int(kind[4:]) <= 14
    if kind.startswith("pkg-"):
        from .plans import card_by_no

        return card_by_no(kind[4:]) is not None
    return False
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
        ("red", "RED · STOP", "Private, keep it out.",
         ["Passwords, PINs, verification codes", "Bank/card numbers, national ID",
          "Home address, other people's data"],
         "→ Never paste. Anonymise, then retry."),
        ("amber", "AMBER · CAUTION", "A little personal.",
         ["Your first name or city", "Your rough plans or preferences", "Non-sensitive everyday details"],
         "→ Use a placeholder or remove it first."),
        ("green", "GREEN · GO", "Public, non-personal.",
         ["General questions & explanations", "Public articles to summarise", "Story, recipe, and idea prompts"],
         "→ Any tool you like — then check facts."),
    ],
}


# ---------- links ----------

def logo_version(org) -> str:
    logo = brand_logo(org)
    return hashlib.sha256((org.logo_data or "").encode()).hexdigest()[:8] if logo is not None else "aidl"


def signature(kind: str, org_id: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"card:{kind}:{org_id}".encode(), hashlib.sha256).hexdigest()[:20]


def image_url(kind: str, org) -> str:
    oid = str(org.pk)
    version = logo_version(org)
    if kind.startswith("aup-"):
        from .aup_store import version as aup_version

        version += "-" + (aup_version(org) or "none")
    return f"{settings.BACKEND_URL}/api/cards/{kind}/{oid}.png?s={signature(kind, oid)}&v={version}"


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


def _dashes(d, y):
    for x in range(COL, W - PAD, 7):
        d.line([_xy(x, y), _xy(min(x + 3, W - PAD), y)], fill=RULE, width=S)


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
    d.text(_xy(x, PAD + 22), "aidrivinglicense.com", font=_font("Bold", 16), fill=INK, anchor="lm")  # as designed
    icon(d, x, PAD + 28)
    d.text(_xy(x + 30, PAD + 47), title, font=_font("Black", 22), fill=INK, anchor="ls")
    return PAD + 62


# ---------- the cards ----------

def _doc_icon(d, x, y) -> None:
    d.rectangle(_xy(x + 2, y, x + 20, y + 23), fill="#FFFFFF", outline=INK, width=S)
    for k in range(4):
        d.line([_xy(x + 6, y + 6 + k * 4), _xy(x + 16, y + 6 + k * 4)], fill=GREY, width=S)


def _tag(d, x, y, text: str, fill: str = "#FFCC00", color: str = INK) -> float:
    font = _font("Black", 11)
    w = d.textlength(text, font=font) / S + 16
    d.rounded_rectangle(_xy(x, y, x + w, y + 20), radius=3 * S, fill=fill)
    d.text(_xy(x + 8, y + 10), text, font=font, fill=color, anchor="lm")
    return y + 30


def _plain(text: str) -> str:
    return " ".join((text or "").replace("*", "").split())


def _aup_point(img, d, org, n: int) -> float:
    from .aup_store import POINTS, current

    aup = current(org)
    y = _header(img, d, org, "AI Acceptable Use Policy", _doc_icon)
    width = W - PAD - COL
    if aup is None or not 1 <= n <= len(aup["points"]):
        return _rich(d, COL, y + 4, width, "The AI policy is still being set up.", size=16) + 10
    point = aup["points"][n - 1]
    y = _tag(d, COL, y + 2, f"POINT {n} OF {POINTS}")
    y = _rich(d, COL, y, width, f"**{_plain(point['title'])}**", size=20, line=28) + 2
    if point.get("intro"):
        y = _rich(d, COL, y, width, _plain(point["intro"]), size=15, line=22, color=GREY) + 6
    clauses = [_plain(c["text"]) for c in point.get("clauses") or []] or ["Nothing for this point."]
    for text in clauses:
        _dashes(d, y)
        d.ellipse(_xy(COL + 4, y + 17, COL + 10, y + 23), fill="#FFCC00", outline=INK)
        y = _rich(d, COL + 22, y + 10, width - 22, text, size=15, line=22) + 8
    return y


def _awareness(img, d, org, no: str) -> float:
    from .plans import card_by_no

    card = card_by_no(no) or {}
    y = _header(img, d, org, "Awareness card", _book_icon)
    width = W - PAD - COL
    y = _tag(d, COL, y + 2, f"{card.get('category', '')} · {card.get('type', '')}".upper()[:70])
    y = _rich(d, COL, y, width, f"**{_plain(card.get('title', ''))}**", size=22, line=30) + 4
    y = _rich(d, COL, y, width, f"**{_plain(card.get('hook', ''))}**", size=17, line=25) + 6
    y = _rich(d, COL, y, width, _plain(card.get("message", "")), size=16, line=24) + 12
    action = _plain(card.get("action", ""))
    if action:
        top = y
        # measure on a scratch canvas, then draw the box behind the text
        scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        bottom = _rich(scratch, COL + 18, top + 12, width - 30, f"**{action}**", size=15, line=22)
        d.rectangle(_xy(COL, top, W - PAD, bottom + 10), fill="#FFF6CC")
        d.rectangle(_xy(COL, top, COL + 4, bottom + 10), fill="#FFCC00")
        _rich(d, COL + 18, top + 12, width - 30, f"**{action}**", size=15, line=22)
        y = bottom + 20
    return y


# Where the designed cards have their logo circle (2x pixels): x, y, size.
_DESIGN_BADGE = (32, 28, 104)


def logo_watermark(img: Image.Image, logo: Image.Image, *, angle: float = 55, width_ratio: float = 1.25,
                   opacity: float = 0.12) -> None:
    """The design's watermark: the logo at 125% of the card's width, turned
    55° (CSS rotate(-55deg)), 12% opacity, centred on top of the card."""
    w = int(img.width * width_ratio)
    mark = logo.copy()
    mark = mark.resize((w, max(1, int(mark.height * w / mark.width))), Image.LANCZOS)
    mark = mark.rotate(angle, expand=True, resample=Image.BICUBIC)
    mark.putalpha(mark.getchannel("A").point(lambda a: int(a * opacity)))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer.paste(mark, ((img.width - mark.width) // 2, (img.height - mark.height) // 2), mark)
    # keep the card's transparent rounded corners transparent
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", img.size, 0), img.getchannel("A")))
    img.alpha_composite(layer)


def _designed(kind: str, org) -> Image.Image:
    img = Image.open(_DIR / "static" / "cards" / f"{kind}.png").convert("RGBA")
    logo = brand_logo(org)
    if logo is not None:
        x, y, size = _DESIGN_BADGE
        d = ImageDraw.Draw(img)
        d.ellipse((x - 2, y - 2, x + size + 2, y + size + 2), fill="#FFFFFF")
        pic = _contain(logo, size - 8, size - 8)
        img.alpha_composite(pic, (x + 4, y + 4))
        if kind == "highway_code":
            logo_watermark(img, logo)
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render(kind: str, org) -> bytes:
    if kind in KINDS:
        return _png(_designed(kind, org))
    img = Image.new("RGBA", _xy(W, 3000), "#FFFFFF")
    d = ImageDraw.Draw(img)
    if kind.startswith("aup-"):
        bottom = _aup_point(img, d, org, int(kind[4:]))
    elif kind.startswith("pkg-"):
        bottom = _awareness(img, d, org, kind[4:])
    else:
        raise ValueError(kind)
    bottom += PAD - 10
    img = img.crop((0, 0, img.width, int(bottom * S)))

    logo = brand_logo(org)
    if logo is not None and kind.startswith("aup-"):
        pass  # slanted, after the rounded corners (below)
    elif logo is not None:  # faint company logo across the card
        size = int(min(img.width * 0.62, img.height * 0.8))
        wm = _contain(logo, size, size)
        wm.putalpha(wm.getchannel("A").point(lambda a: int(a * 0.07)))
        img.alpha_composite(wm, (img.width - size - 24 * S, (img.height - size) // 2))

    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=8 * S, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    if logo is not None and kind.startswith("aup-"):
        logo_watermark(out, logo)
    return _png(out)


def alt_text(kind: str, org=None) -> str:
    """What screen readers (and Slack notifications) get instead of the image."""
    if kind.startswith("aup-") and org is not None:
        from .aup_store import current

        aup = current(org)
        n = int(kind[4:])
        if aup is None or n > len(aup["points"]):
            return "AI Acceptable Use Policy."
        point = aup["points"][n - 1]
        text = f"AUP point {n}: {_plain(point['title'])}. " + " ".join(_plain(c["text"]) for c in point.get("clauses") or [])
        return text[:1990]
    if kind.startswith("pkg-"):
        from .plans import card_by_no

        c = card_by_no(kind[4:]) or {}
        return _plain(f"{c.get('title', '')}. {c.get('hook', '')} {c.get('message', '')} {c.get('action', '')}")[:1990]
    if kind == "highway_code":
        return "The Highway Code. " + " ".join(f"{t}: {b.replace('**', '')}" for _k, t, b in HIGHWAY["rules"])
    return "Traffic Light Check. " + " ".join(
        f"{title}, {sub} For example: {', '.join(items)}. {action.lstrip('→ ')}"
        for _n, title, sub, items, action in TRAFFIC["lights"])
