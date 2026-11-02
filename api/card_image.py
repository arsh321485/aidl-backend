"""AIDL cards as PNGs for Slack image blocks — one design language for the
whole library (design: E:\\AIDL Card Design System.html), drawn with Pillow:

    frame    square card, 3px ink outline, hard shadow, hazard stripe on top
    header   badge + mono label line + Bungee title. The badge shape says what
             kind of card it is, like road signs: diamond = policy ("aup-<n>",
             aup_store / policy_cards.py), circle = onboarding (traffic_light,
             highway_code), square = awareness ("pkg-<card no>", and the AI
             policy's "paw-<id>", policy_awareness.py)
    action   whatever the card asks people to do is the same sign
    footer   AIDL disc + AI DRIVING LICENSE + the button to press
    mark     Basic and Enterprise: the company logo, else the company name,
             slanted and faint. Trial cards show AIDL only
             (licence_image.brand_logo).
    AIDL     policy cards and the Highway Code also carry the official AIDL
             logo (static/aidl/logo-transparent.png): in the header, the footer
             and, faint, behind the card's content (_branded).

The body changes with the content; policy cards pick one of four layouts
(_layout). The approved words are never rewritten: lists from the question
bank get one line per item, anything else is shown line by line as approved.

Served at GET /api/cards/<kind>/<org id>.png?s=<signature>&v=<version>
(licence_views.card_png). `v` changes with the design, the company's brand
and the AUP version, so Slack fetches the new image after any of them changes.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from PIL import Image, ImageColor, ImageDraw, ImageFont

from .licence_image import brand_logo, text_watermark

KINDS = ("highway_code", "traffic_light")  # + "aup-<n>" and "pkg-<card no>"
# Bump when a card's look changes: the image links change, so Slack fetches
# the new pictures instead of showing the copies it already has.
DESIGN_VERSION = "7"


def valid_kind(kind: str) -> bool:
    if kind in KINDS:
        return True
    if kind.startswith("aup-"):
        return kind[4:].isdigit() and 1 <= int(kind[4:]) <= 14
    if kind.startswith("pkg-"):
        from .plans import card_by_no

        return card_by_no(kind[4:]) is not None
    if kind.startswith("paw-"):
        from .policy_awareness import by_id

        return by_id(kind[4:]) is not None
    return False
S = 2  # draw at 2x for a crisp image
W, PAD = 780, 30
SHADOW = 6
INK, WHITE, CREAM, CREAM2, YELLOW, YELLOW_SOFT = "#14140F", "#FFFFFF", "#F5ECD2", "#F0E3BD", "#FFCC00", "#FFF3B8"
GREEN, RED, AMBER, SKY, MUTED = "#2EC866", "#E23A2E", "#FFB000", "#6FB3E0", "#5E5843"
GREEN_DK, RED_DK = "#1A7F37", "#B42318"
_FONTS = Path(__file__).resolve().parent / "data" / "fonts"
_AIDL_LOGO = Path(__file__).resolve().parent / "static" / "aidl" / "logo-transparent.png"
HEADER_LOGO, FOOTER_LOGO, WATERMARK_LOGO = 66, 28, 300  # widths, the same on every card
WATERMARK_OPACITY = 0.06
LOGO_EDGE = W - PAD - HEADER_LOGO - 12

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
    "remember": "AI drafts; you decide. Check it, own it, then use it.",
}

TRAFFIC = {
    "title": "Traffic Light Check",
    "intro": "Before you paste anything into an AI, check the lights.",
    "lights": [
        ("RED · STOP", "Private, keep it out.", RED, "#FDECEA", "#FFFFFF",
         ["Passwords, PINs, verification codes", "Bank/card numbers, national ID",
          "Home address, other people's data"],
         "Never paste. Anonymise, then retry."),
        ("AMBER · CAUTION", "A little personal.", AMBER, "#FFF6DD", INK,
         ["Your first name or city", "Your rough plans or preferences", "Non-sensitive everyday details"],
         "Use a placeholder or remove it first."),
        ("GREEN · GO", "Public, non-personal.", GREEN, "#EAF8EF", INK,
         ["General questions & explanations", "Public articles to summarise", "Story, recipe, and idea prompts"],
         "Any tool you like — then check facts."),
    ],
}

# The awareness library's 12 types, as the two letters in the square badge.
TYPE_MARK = {"Micro-Lesson": "ML", "Field Guide": "FG", "Checklist": "CL", "Gate Briefing": "GB", "Decision Aid": "DA",
             "Policy": "PO", "Template": "TP", "Reference Card": "RC", "Registry": "RG", "Worked Examples": "WE",
             "Drill": "DR", "Assessment": "AS"}
# Policy cards: the layout each topic prefers (see _layout).
TOPIC_LAYOUT = {1: "D", 3: "D", 4: "D", 11: "D", 2: "A", 5: "A", 6: "A", 8: "A", 9: "A", 7: "B", 10: "B",
                12: "C", 13: "C", 14: "C"}


# ---------- links ----------

def _paid(org) -> bool:
    return getattr(org, "plan", "trial") in ("basic", "enterprise")


def logo_version(org) -> str:
    """Changes with what the company's cards show: its logo and name."""
    if not _paid(org):
        return "aidl"
    return hashlib.sha256(f"{org.logo_data or ''}|{org.name}".encode()).hexdigest()[:8]


def signature(kind: str, org_id: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"card:{kind}:{org_id}".encode(), hashlib.sha256).hexdigest()[:20]


def version_for(kind: str, org) -> str:
    """Everything that changes this picture: design, logo, AUP version."""
    version = f"d{DESIGN_VERSION}-{logo_version(org)}"
    if kind.startswith("aup-"):
        from .aup_store import card_version

        version += "-" + (card_version(org, int(kind[4:])) or "none")
    return version


def image_url(kind: str, org) -> str:
    oid = str(org.pk)
    return f"{settings.BACKEND_URL}/api/cards/{kind}/{oid}.png?s={signature(kind, oid)}&v={version_for(kind, org)}"


def png_for(kind: str, org) -> bytes:
    """The card's PNG — drawn once, then served from the cache (image_cache)."""
    from .image_cache import png

    return png(f"card:{org.pk}:{kind}:{version_for(kind, org)}", lambda: render(kind, org))


def onboarding_kinds(org, item: str) -> list[str]:
    if item == "aup":
        from .aup_store import cards

        return [f"aup-{n}" for n in sorted(cards(org))]
    return [item]


def warm(org, kinds: list[str]) -> None:
    """Draw these cards now (in the background) so Slack gets them at once."""
    from .image_cache import warm_in_background

    def run(org_, kinds_):
        for kind in kinds_:
            png_for(kind, org_)

    if kinds:
        warm_in_background(run, org, list(kinds))


# ---------- drawing helpers ----------

@lru_cache(maxsize=48)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """sign = Bungee, mono = JetBrains Mono, text / medium / bold = Space Grotesk."""
    if kind == "sign":
        return ImageFont.truetype(str(_FONTS / "Bungee-Regular.ttf"), size * S)
    if kind == "mono":
        return ImageFont.truetype(str(_FONTS / "JetBrainsMono-Bold.ttf"), size * S)
    font = ImageFont.truetype(str(_FONTS / "SpaceGrotesk.ttf"), size * S)
    font.set_variation_by_name({"text": "Regular", "medium": "Medium", "bold": "Bold"}[kind])
    return font


def _xy(*v):
    return tuple(int(round(x * S)) for x in v)


def _plain(text: str) -> str:
    return " ".join((text or "").replace("*", "").split())


def _sentence(text: str) -> str:
    text = text.strip()
    text = text[:1].upper() + text[1:]
    return text if text[-1:] in ".!?" or text[-2:-1] in (".", "!", "?") else text + "."


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _para(d, x, y, text, kind, size, width, fill=INK, line=None) -> float:
    """Wrap `text` into `width`; returns the y below it."""
    font = _font(kind, size)
    line = line or size * 1.38
    row = ""
    for word in text.split():
        trial = f"{row} {word}".strip()
        if row and d.textlength(trial, font=font) > width * S:
            d.text(_xy(x, y), row, font=font, fill=fill)
            y += line
            row = word
        else:
            row = trial
    if row:
        d.text(_xy(x, y), row, font=font, fill=fill)
        y += line
    return y


def _rich(d, x, y, text, size, width, line) -> float:
    """Like _para, for text with **bold** runs."""
    space = d.textlength(" ", font=_font("text", size)) / S
    cx = x
    for i, part in enumerate(text.split("**")):
        font = _font("bold" if i % 2 else "text", size)
        for word in part.split():
            w = d.textlength(word, font=font) / S
            if cx > x and cx + w > x + width:
                cx, y = x, y + line
            d.text(_xy(cx, y), word, font=font, fill=INK)
            cx += w + space
    return y + line


def _measure(fn, *args) -> float:
    return fn(ImageDraw.Draw(Image.new("RGBA", (4, 4))), *args)


def _rule(d, y) -> None:
    d.line(_xy(PAD, y, W - PAD, y), fill=INK, width=2 * S)


def _tag(d, x, y, text, bg, fg=INK, size=12) -> float:
    font = _font("mono", size)
    w = d.textlength(text, font=font) / S + 14
    d.rectangle(_xy(x, y, x + w, y + size + 10), fill=bg, outline=INK, width=2 * S)
    d.text(_xy(x + 7, y + (size + 10) / 2 + 0.5), text, font=font, fill=fg, anchor="lm")
    return x + w


def _tick(d, x, y) -> None:
    d.line([_xy(x + 1, y + 8), _xy(x + 5, y + 13), _xy(x + 14, y + 1)], fill=GREEN_DK, width=3 * S, joint="curve")


def _cross(d, x, y) -> None:
    d.line([_xy(x + 2, y + 2), _xy(x + 13, y + 13)], fill=RED_DK, width=3 * S)
    d.line([_xy(x + 13, y + 2), _xy(x + 2, y + 13)], fill=RED_DK, width=3 * S)


def _badge(d, shape: str, label: str) -> None:
    """Diamond = policy, circle = onboarding, square = awareness."""
    cx, cy, r = PAD + 32, 62, 30
    if shape == "policy":
        pts = [(cx, cy - r - 4), (cx + r + 4, cy), (cx, cy + r + 4), (cx - r - 4, cy)]
        d.polygon([_xy(px + 3, py + 3) for px, py in pts], fill=INK)
        d.polygon([_xy(*p) for p in pts], fill=YELLOW, outline=INK, width=3 * S)
    elif shape == "onboarding":
        d.ellipse(_xy(cx - r + 3, cy - r + 3, cx + r + 3, cy + r + 3), fill=INK)
        d.ellipse(_xy(cx - r, cy - r, cx + r, cy + r), fill=YELLOW, outline=INK, width=3 * S)
    else:
        d.rectangle(_xy(cx - r + 3, cy - r + 3, cx + r + 3, cy + r + 3), fill=INK)
        d.rectangle(_xy(cx - r, cy - r, cx + r, cy + r), fill=YELLOW, outline=INK, width=3 * S)
    d.text(_xy(cx, cy + 1), label, font=_font("sign", 20), fill=INK, anchor="mm")


def _header(d, shape, label, eyebrow, title, right="", sub="", edge=W - PAD) -> float:
    """The same header on every card; returns the y below it. `edge` is where
    its text stops (LOGO_EDGE when the AIDL logo sits on the right)."""
    _badge(d, shape, label)
    x = PAD + 84
    d.text(_xy(x, 30), eyebrow.upper(), font=_font("mono", 11), fill=MUTED)
    if right:
        d.text(_xy(edge, 30), right.upper(), font=_font("mono", 11), fill=MUTED, anchor="ra")
    y = _para(d, x, 46, title.upper(), "sign", 24, edge - x, line=30)
    if sub:
        y = _para(d, x, y + 2, sub.upper(), "mono", 10, W - x - PAD, fill=MUTED, line=16)
    return max(y, 100) + 14


def _sign(d, y, label, draw_body, fill=YELLOW, label_fill=None, x0=PAD, x1=W - PAD) -> float:
    """The one way every card says what to do: an ink label on a coloured
    sign. draw_body(d, x, top, width) draws the inside and returns its bottom."""
    font = _font("mono", 11)
    lw = d.textlength(label, font=font) / S + 18
    x = x0 + lw + 14
    inner = x1 - x - 14
    h = _measure(draw_body, x, 0, inner) + 10
    d.rectangle(_xy(x0 + 4, y + 4, x1 + 4, y + h + 4), fill=INK)
    d.rectangle(_xy(x0, y, x1, y + h), fill=fill, outline=INK, width=3 * S)
    d.rectangle(_xy(x0, y, x0 + lw, y + h), fill=INK)
    d.text(_xy(x0 + lw / 2, y + h / 2 + 1), label, font=font, fill=label_fill or fill, anchor="mm")
    draw_body(d, x, y, inner)
    return y + h + 4


def _action(d, y, text, label="DO THIS", fill=YELLOW, fg=INK, **kw) -> float:
    return _sign(d, y, label, lambda dd, x, top, width: _para(dd, x, top + 10, text, "bold", 17, width, fill=fg, line=24),
                 fill=fill, **kw)


def _action_list(d, y, items, label) -> float:
    """The action sign holding several lines."""
    def body(dd, x, top, width):
        yy = top + 10
        for item in items:
            dd.text(_xy(x, yy), "›", font=_font("bold", 17), fill=INK)
            yy = _para(dd, x + 18, yy, item, "bold", 17, width - 18, line=24) + 4
        return yy
    return _sign(d, y, label, body, fill=YELLOW_SOFT, label_fill=YELLOW)


def _footer(d, y, press: str, disc: bool = True) -> float:
    _rule(d, y)
    if disc:  # else _add_logos puts the AIDL logo here
        d.ellipse(_xy(PAD, y + 12, PAD + 24, y + 36), fill=YELLOW, outline=INK, width=3 * S)
        d.text(_xy(PAD + 12, y + 24.5), "AI", font=_font("sign", 9), fill=INK, anchor="mm")
    d.text(_xy(PAD + 38, y + 24), "AIDL · AI DRIVING LICENSE", font=_font("mono", 10), fill=MUTED, anchor="lm")
    d.text(_xy(W - PAD, y + 24), f"PRESS  {press}  BELOW", font=_font("mono", 10), fill=MUTED, anchor="rm")
    return y + 48


# ---------- policy cards ----------

_STOP = re.compile(r"\b(must not|never|not allowed|prohibited|may not|do not|don't|not permitted|must never)\b", re.I)
_GO = re.compile(r"\b(may|allowed|permitted|can)\b", re.I)
_SENTENCES = re.compile(r"(?<=\.)\s+(?=[A-Z])")


def _statements(rules: list[tuple]) -> list:
    """Every rule as a single statement; approved AI tools come as ("tool", name)."""
    out = []
    for rule in rules:
        if rule[0] == "text":
            out += [s.strip() for s in _SENTENCES.split(rule[1])]
        else:
            out += [("tool", i) if rule[1] == "Approved AI tools" else i for i in rule[2]]
    return out


def _go_stop(statement) -> str:
    """go / stop / always, from the wording ("may" / "must not" / the rest)."""
    if isinstance(statement, tuple):
        return "go"
    if statement.lower().startswith("only "):
        return "always"
    if _STOP.search(statement):
        return "stop"
    return "go" if _GO.search(statement) and "must" not in statement.lower() else "always"


def _uploaded(rules: list[tuple]) -> tuple[list, list, list] | None:
    """An uploaded policy's card (aup_upload.to_points): (do, don't, the rest),
    when it has both."""
    if any(r[0] != "text" for r in rules):
        return None
    texts = [r[1] for r in rules]
    go = [t[4:] for t in texts if t.startswith("Do: ")]
    stop = [t[7:] for t in texts if t.startswith("Don't: ")]
    rest = [t for t in texts if not t.startswith(("Do: ", "Don't: "))]
    return (go, stop, rest) if go and stop else None


def _big_rule(rules: list[tuple]) -> list[str]:
    out = []
    for rule in rules:
        out += [s.strip() for s in _SENTENCES.split(rule[1])] if rule[0] == "text" else [rule[3]]
    return out


def _layout(n: int, rules: list[tuple]) -> str:
    """D one big rule · A numbered rules · B Go / Stop · C handbook page.
    Picked from the topic and the card's content, never by the admin."""
    if _uploaded(rules):
        return "B"
    want = TOPIC_LAYOUT[n]
    if want == "B" and not {"go", "stop"} <= {_go_stop(s) for s in _statements(rules)}:
        return "A"
    if want == "D" and len(_big_rule(rules)) > 4:
        return "A"
    return want


def _draw_numbered(d, y, rules) -> float:
    for num, rule in enumerate(rules, 1):
        _rule(d, y)
        y += 12
        d.rectangle(_xy(PAD, y + 2, PAD + 26, y + 28), fill=INK)
        d.text(_xy(PAD + 13, y + 15), str(num), font=_font("mono", 13), fill=YELLOW, anchor="mm")
        x = PAD + 42
        if rule[0] == "text":
            y = _para(d, x, y, rule[1], "text", 17, W - x - PAD) + 10
        else:
            y = _para(d, x, y, rule[1], "bold", 17, W - x - PAD) + 2
            for item in rule[2]:
                d.text(_xy(x + 4, y), "–", font=_font("text", 16), fill=MUTED)
                y = _para(d, x + 22, y, item, "text", 16, W - x - PAD - 22, line=22.5)
            y += 10
    return y + 4


def _draw_go_stop(d, y, go, stop, always, label) -> float:
    _rule(d, y)
    top = y + 16
    col = (W - 2 * PAD - 22) / 2
    bottoms = []
    for name, items, bg, fg, x, mark in (("GO", go, GREEN, INK, PAD, _tick),
                                         ("STOP", stop, RED, WHITE, PAD + col + 22, _cross)):
        yy = top
        _tag(d, x, yy, name, bg, fg)
        yy += 34
        for item in items:
            mark(d, x, yy + 4)
            yy = _para(d, x + 24, yy, _sentence(item), "text", 16, col - 26, line=22.5) + 8
        bottoms.append(yy)
    d.line(_xy(PAD + col + 11, top, PAD + col + 11, max(bottoms)), fill=INK, width=2 * S)
    y = max(bottoms) + 8
    if always:
        y = _action_list(d, y, [_sentence(s) for s in always], label) + 14
    return y


def _draw_handbook(d, y, rules) -> float:
    font = _font("mono", 11)
    w = d.textlength("REQUIRED", font=font) / S + 16
    d.rectangle(_xy(LOGO_EDGE - w, 50, LOGO_EDGE, 72), fill=WHITE, outline=RED, width=2 * S)
    d.text(_xy(LOGO_EDGE - w / 2, 61), "REQUIRED", font=font, fill=RED, anchor="mm")
    first, rest = rules[0], rules[1:]
    inner = W - 2 * PAD - 36

    def key_rule(dd, top):
        if first[0] == "text":
            return _para(dd, PAD + 18, top + 14, first[1], "bold", 18, inner, line=25) + 14
        yy = _para(dd, PAD + 18, top + 14, first[1] + ":", "bold", 18, inner, line=25) + 4
        for item in first[2]:
            dd.text(_xy(PAD + 20, yy), "–", font=_font("text", 16), fill=MUTED)
            yy = _para(dd, PAD + 40, yy, item, "text", 16, inner - 22, line=22.5)
        return yy + 14

    h = _measure(key_rule, y) - y
    d.rectangle(_xy(PAD, y, W - PAD, y + h), fill=CREAM, outline=INK, width=3 * S)
    key_rule(d, y)
    y += h + 16
    width = W - 2 * PAD
    for rule in rest:
        d.text(_xy(PAD + 2, y), "›", font=_font("bold", 18), fill=RED)
        if rule[0] == "text":
            y = _para(d, PAD + 22, y, rule[1], "text", 16, width - 22, line=23) + 8
        else:
            y = _para(d, PAD + 22, y, rule[1] + ":", "bold", 16, width - 22, line=23) + 2
            for item in rule[2]:
                d.text(_xy(PAD + 26, y), "–", font=_font("text", 15), fill=MUTED)
                y = _para(d, PAD + 44, y, item, "text", 15, width - 44, line=21.5)
            y += 8
    return y


def _draw_big_rule(d, y, rules) -> float:
    head, *support = _big_rule(rules)
    _rule(d, y)
    y = _para(d, PAD, y + 18, head, "bold", 30, W - 2 * PAD, line=38) + 10
    d.rectangle(_xy(PAD, y, PAD + 80, y + 8), fill=YELLOW, outline=INK, width=2 * S)
    y += 26
    for line in support:
        _rule(d, y)
        y = _para(d, PAD + 10, y + 12, f"›  {line}", "text", 17, W - 2 * PAD - 10, line=24) + 10
    return y


def _rail(d, y, n) -> float:
    """Where this point sits among the 14."""
    seg = (W - 2 * PAD - 13 * 4) / 14
    for i in range(14):
        x = PAD + i * (seg + 4)
        lit = i + 1 == n
        d.rectangle(_xy(x, y, x + seg, y + 8), fill=YELLOW if lit else CREAM2, outline=INK if lit else CREAM2,
                    width=2 * S)
    return y + 22


def _policy_card(d, org, n: int) -> float:
    from .aup_store import card
    from .policy_cards import SHORT, card_rules

    who = org.name if _paid(org) else "AIDL"
    y = _header(d, "policy", str(n), f"{_short(who, 28)} · AI policy · point {n} of 14", SHORT[n], edge=LOGO_EDGE)
    saved = card(org, n)
    rules = card_rules(org, n, saved["lines"]) if saved else []
    if not rules:
        _rule(d, y)
        y = _para(d, PAD, y + 16, "The AI policy is still being set up.", "text", 17, W - 2 * PAD) + 10
    else:
        layout = _layout(n, rules)
        if layout == "A":
            y = _draw_numbered(d, y, rules)
        elif layout == "C":
            y = _draw_handbook(d, y, rules)
        elif layout == "D":
            y = _draw_big_rule(d, y, rules)
        elif _uploaded(rules):
            go, stop, rest = _uploaded(rules)
            y = _draw_go_stop(d, y, go, stop, rest, "IN SHORT")
        else:
            sts = _statements(rules)
            y = _draw_go_stop(d, y, [f"Use {s[1]}" if isinstance(s, tuple) else s for s in sts if _go_stop(s) == "go"],
                              [s for s in sts if _go_stop(s) == "stop"],
                              [s for s in sts if _go_stop(s) == "always"], "ALWAYS")
    return _footer(d, _rail(d, y + 6, n), "I AGREE", disc=False)


# ---------- onboarding ----------

def _lighter(colour: str) -> tuple[int, ...]:
    return tuple(v + (255 - v) * 35 // 100 for v in ImageColor.getrgb(colour))


def _traffic_light(d, org) -> float:
    y = _header(d, "onboarding", "1", "AIDL · onboarding · card 1 of 2", TRAFFIC["title"])
    y = _para(d, PAD, y - 4, TRAFFIC["intro"], "medium", 19, W - 2 * PAD) + 10
    _rule(d, y)
    y += 20
    left = 168
    rx, row, gap = PAD + left + 22, 195, 16
    rw = W - rx - PAD
    total = 3 * row + 2 * gap
    cx = PAD + left / 2
    x0, x1 = PAD + 14, PAD + left - 14
    d.rectangle(_xy(cx - 10, y + total - 4, cx + 10, y + total + 26), fill=INK)
    d.rounded_rectangle(_xy(x0 + 5, y + 5, x1 + 5, y + total + 5), radius=20 * S, fill=INK)
    d.rounded_rectangle(_xy(x0, y, x1, y + total), radius=20 * S, fill="#22221C", outline=INK, width=3 * S)
    yy = y
    for label, sub, col, tint, action_fg, items, action in TRAFFIC["lights"]:
        cy, r = yy + row / 2, 58
        d.ellipse(_xy(cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8), fill="#111109")
        d.ellipse(_xy(cx - r, cy - r, cx + r, cy + r), fill=col, outline=INK, width=3 * S)
        d.ellipse(_xy(cx - r * 0.6, cy - r * 0.66, cx - r * 0.1, cy - r * 0.28), fill=_lighter(col))
        d.rectangle(_xy(rx, yy, rx + rw, yy + row), fill=tint, outline=INK, width=2 * S)
        d.rectangle(_xy(rx, yy, rx + 8, yy + row), fill=col)
        ty = yy + 14
        after = _tag(d, rx + 22, ty, label, col, WHITE if col == RED else INK)
        d.text(_xy(after + 12, ty + 11), sub, font=_font("bold", 18), fill=INK, anchor="lm")
        ty += 40
        for item in items:
            d.text(_xy(rx + 24, ty), "•", font=_font("bold", 17), fill="#B37A00" if col == AMBER else col)
            d.text(_xy(rx + 42, ty), item, font=_font("text", 17), fill=INK)
            ty += 25
        _action(d, ty + 6, action, fill=col, fg=action_fg, x0=rx + 22, x1=rx + rw - 18)
        yy += row + gap
    return _footer(d, y + total + 34, "GOT IT")


def _road_sign(d, cx, cy, shape: str) -> None:
    r = 34
    if shape == "stop":
        pts = [(cx + r * math.cos(math.radians(22.5 + 45 * i)), cy + r * math.sin(math.radians(22.5 + 45 * i)))
               for i in range(8)]
        d.polygon([_xy(*p) for p in pts], fill=RED, outline=INK, width=3 * S)
        d.text(_xy(cx, cy + 1), "STOP", font=_font("sign", 12), fill=WHITE, anchor="mm")
    elif shape == "check":
        d.polygon([_xy(cx, cy - r), _xy(cx + r, cy + r * 0.8), _xy(cx - r, cy + r * 0.8)], fill=WHITE, outline=RED,
                  width=5 * S)
        d.text(_xy(cx, cy + 9), "!", font=_font("sign", 22), fill=INK, anchor="mm")
    elif shape == "you":
        d.polygon([_xy(cx - r, cy - r * 0.8), _xy(cx + r, cy - r * 0.8), _xy(cx, cy + r)], fill=WHITE, outline=RED,
                  width=5 * S)
        d.text(_xy(cx, cy - 8), "YOU", font=_font("sign", 11), fill=INK, anchor="mm")
    elif shape == "ask":
        d.ellipse(_xy(cx - r, cy - r, cx + r, cy + r), fill=SKY, outline=INK, width=3 * S)
        d.text(_xy(cx, cy + 2), "?", font=_font("sign", 30), fill=WHITE, anchor="mm")
    else:  # one way
        d.rectangle(_xy(cx - r - 4, cy - 18, cx + r + 4, cy + 18), fill=SKY, outline=INK, width=3 * S)
        d.polygon([_xy(cx - r + 4, cy - 5), _xy(cx + 12, cy - 5), _xy(cx + 12, cy - 12), _xy(cx + r - 2, cy),
                   _xy(cx + 12, cy + 12), _xy(cx + 12, cy + 5), _xy(cx - r + 4, cy + 5)], fill=WHITE)


def _highway_code(d, org) -> float:
    y = _header(d, "onboarding", "2", "AIDL · onboarding · card 2 of 2 · section A", HIGHWAY["title"],
                right="learner rules", edge=LOGO_EDGE)
    y = _para(d, PAD, y - 4, HIGHWAY["intro"], "medium", 18, W - 2 * PAD, line=25) + 10
    x = PAD + 104
    for i, (shape, title, body) in enumerate(HIGHWAY["rules"], 1):
        _rule(d, y)
        top = y + 16
        yy = _para(d, x, top, f"{i}. {title}", "bold", 19, W - x - PAD, line=26) + 2
        yy = _rich(d, x, yy, body, 16, W - x - PAD, 23) + 6
        _road_sign(d, PAD + 44, (top + yy) / 2 - 2, shape)
        y = max(yy, top + 84) + 4
    y = _action(d, y + 6, HIGHWAY["remember"], label="REMEMBER") + 18
    return _footer(d, y, "GOT IT", disc=False)


# ---------- awareness ----------

def _awareness(d, org, no: str) -> float:
    from .plans import card_by_no

    card = card_by_no(no) or {}
    kind = card.get("type", "")
    y = _header(d, "awareness", TYPE_MARK.get(kind, "AI"), f"Awareness · {kind} · card {no}",
                _plain(card.get("title", "")), right=_short(org.name, 24) if _paid(org) else "",
                sub=card.get("category", ""))
    _rule(d, y)
    y = _para(d, PAD, y + 16, _plain(card.get("hook", "")), "bold", 22, W - 2 * PAD, line=30) + 8
    y = _para(d, PAD, y, _plain(card.get("message", "")), "text", 17, W - 2 * PAD, line=25) + 14
    action = re.sub(r"^do this\s*:\s*", "", _plain(card.get("action", "")), flags=re.I)
    if action:
        y = _action(d, y, _sentence(action)) + 18
    return _footer(d, y, "GOT IT")


def _policy_awareness(d, org, card_id: str) -> float:
    """A ready-made awareness card from the AI policy (policy_awareness.py)."""
    from .policy_awareness import by_id

    card = by_id(card_id) or {"title": "", "lines": []}
    y = _header(d, "awareness", "AP", "Awareness · from your AI policy", card["title"],
                right=_short(org.name, 24) if _paid(org) else "")
    _rule(d, y)
    y += 14
    lines = [_plain(line) for line in card["lines"]]
    if lines and lines[0].endswith(":"):
        y = _para(d, PAD, y, lines.pop(0), "bold", 18, W - 2 * PAD, line=25) + 6
    closing = lines.pop() if lines and lines[-1].startswith(("Not sure", "Your screen")) else ""
    for line in lines:
        d.text(_xy(PAD + 2, y), "›", font=_font("bold", 18), fill=RED)
        y = _para(d, PAD + 22, y, line, "text", 17, W - 2 * PAD - 22, line=24) + 6
    if closing:
        y = _action(d, y + 8, closing, label="REMEMBER") + 10
    return _footer(d, y + 10, "GOT IT")


# ---------- the finished card ----------

def logo_watermark(img: Image.Image, logo: Image.Image, *, angle: float = 28, width_ratio: float = 0.7,
                   opacity: float = 0.10) -> None:
    """The company logo, slanted and faint, across `img`."""
    w = int(img.width * width_ratio)
    mark = logo.copy()
    mark = mark.resize((w, max(1, int(mark.height * w / mark.width))), Image.LANCZOS)
    mark = mark.rotate(angle, expand=True, resample=Image.BICUBIC)
    mark.putalpha(mark.getchannel("A").point(lambda a: int(a * opacity)))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer.paste(mark, ((img.width - mark.width) // 2, (img.height - mark.height) // 2), mark)
    img.alpha_composite(layer)


def _branded(kind: str) -> bool:
    """The cards that carry the AIDL logo and watermark."""
    return kind == "highway_code" or kind.startswith("aup-")


@lru_cache(maxsize=4)
def _aidl_logo(width: int, opacity: float = 1.0) -> Image.Image:
    logo = Image.open(_AIDL_LOGO).convert("RGBA")
    logo = logo.crop(logo.getbbox())
    logo = logo.resize((width * S, round(logo.height * width * S / logo.width)), Image.LANCZOS)
    if opacity < 1:
        logo.putalpha(logo.getchannel("A").point(lambda a: int(a * opacity)))
    return logo


def _aidl_watermark(img: Image.Image) -> None:
    """The AIDL logo, faint, in the middle of the card (drawn first, so the
    text and boxes sit on top of it)."""
    mark = _aidl_logo(WATERMARK_LOGO, WATERMARK_OPACITY)
    img.alpha_composite(mark, ((img.width - mark.width) // 2, max(0, (img.height - mark.height) // 2)))


def _add_logos(img: Image.Image) -> None:
    """The AIDL logo in the header (level with the badge) and the footer."""
    head = _aidl_logo(HEADER_LOGO)
    img.alpha_composite(head, ((W - PAD) * S - head.width, 62 * S - head.height // 2))
    foot = _aidl_logo(FOOTER_LOGO)
    img.alpha_composite(foot, (PAD * S, img.height - 24 * S - foot.height // 2))


def _watermark(body: Image.Image, org) -> None:
    """Basic and Enterprise: the company's logo, else its name. Trial: none."""
    if not _paid(org):
        return
    logo = brand_logo(org)
    if logo is not None:
        logo_watermark(body, logo)
    else:
        text_watermark(body, org.name, angle=28, opacity=0.04, width_ratio=0.62, spacing=0.02,
                       font=ImageFont.truetype(str(_FONTS / "Bungee-Regular.ttf"), 200))


def _frame(body: Image.Image) -> Image.Image:
    """Outline, hard shadow and the hazard stripe around the drawn card."""
    out = Image.new("RGBA", (body.width + (SHADOW + 2) * S, body.height + (SHADOW + 2) * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    bottom = body.height / S
    d.rectangle(_xy(SHADOW, SHADOW, W + SHADOW, bottom + SHADOW), fill=INK)
    out.paste(body, (0, 0))
    stripe = Image.new("RGBA", _xy(W, 12), YELLOW)
    sd = ImageDraw.Draw(stripe)
    for i in range(-1, W // 28 + 1):
        x = i * 28
        sd.polygon([_xy(x, 12), _xy(x + 12, 0), _xy(x + 26, 0), _xy(x + 14, 12)], fill=INK)
    out.paste(stripe, (0, 0))
    d.line(_xy(0, 12, W, 12), fill=INK, width=3 * S)
    d.rectangle(_xy(0, 0, W, bottom), outline=INK, width=3 * S)
    return out


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=6)  # optimize=True takes seconds on the server
    return buf.getvalue()


def _draw(kind: str, d, org) -> float:
    if kind == "traffic_light":
        return _traffic_light(d, org)
    if kind == "highway_code":
        return _highway_code(d, org)
    if kind.startswith("aup-"):
        return _policy_card(d, org, int(kind[4:]))
    if kind.startswith("pkg-"):
        return _awareness(d, org, kind[4:])
    if kind.startswith("paw-"):
        return _policy_awareness(d, org, kind[4:])
    raise ValueError(kind)


def render(kind: str, org) -> bytes:
    bottom = _measure(lambda d: _draw(kind, d, org))
    body = Image.new("RGBA", (W * S, int(bottom * S)), WHITE)
    if _branded(kind):
        _aidl_watermark(body)
    _draw(kind, ImageDraw.Draw(body), org)
    if _branded(kind):
        _add_logos(body)
    _watermark(body, org)
    return _png(_frame(body))


def alt_text(kind: str, org=None) -> str:
    """The image's alt text: just the card's name. Slack shows it as the
    tooltip when the mouse is over the image, so it stays short (it can't be
    empty); the card's words are in the image and the message text."""
    if kind.startswith("aup-"):
        from .policy_cards import SHORT

        return f"Policy card · {SHORT.get(int(kind[4:]), 'AI policy')}"
    if kind.startswith("paw-"):
        from .policy_awareness import by_id

        return (by_id(kind[4:]) or {}).get("title") or "AI awareness"
    if kind.startswith("pkg-"):
        from .plans import card_by_no

        return _plain((card_by_no(kind[4:]) or {}).get("title", "")) or "Awareness card"
    if kind == "highway_code":
        return "The Highway Code"
    return "Traffic Light Check"
