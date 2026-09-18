"""Adaptive Card JSON builders for AIDL Microsoft Teams tabs."""

from django.conf import settings

TEAMS_TABS = (
    ("home", "Home", "🏠"),
    ("learners-permit", "Learner's Permit", "🪪"),
    ("highway-code", "Highway Code", "📖"),
    ("traffic-light-check", "Traffic Light Check", "🚦"),
)

DEFAULT_POLICY_ENTITIES = (
    "SpinifexIT Global Pty Ltd",
    "SpinifexIT North America Inc.",
    "SpinifexIT Solutions UK Limited",
    "SpinifexIT Philippines Inc.",
    "SpinifexIT Singapore Pte. Ltd.",
    "SpinifexIT Deutschland GmbH",
)

# 1x1 solid-color PNGs, tiled via backgroundImage — the same trick the Admin
# Center card uses to get exact brand colors Adaptive Cards' themed style
# enum can't otherwise produce (Container "style" is host-themed, not a hex).
# Only used for colors with no good native-style match (purple brand accent,
# license-card yellow); traffic-light/highway-code rows use native
# good/warning/attention/accent styles instead, since those stay legible in
# both light and dark Teams themes without us guessing at tint colors.
_PURPLE_BG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAA"
    "DElEQVR42mOIjj8OAAKaAYIA57ndAAAAAElFTkSuQmCC"
)
_YELLOW_BG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAA"
    "DElEQVR42mP4dEIQAAR7Acw2oylWAAAAAElFTkSuQmCC"
)


def first_name(full_name: str) -> str:
    parts = (full_name or "").strip().split()
    return parts[0] if parts else "there"


def org_display_name(org_name: str = "") -> str:
    value = (org_name or "").strip()
    if value:
        return value
    return (
        getattr(settings, "AIDL_ORG_DISPLAY_NAME", None) or "Northwind Logistics"
    ).strip() or "Northwind Logistics"


def policy_entities() -> tuple[str, ...]:
    configured = getattr(settings, "AIDL_POLICY_ENTITIES", None)
    if configured:
        return tuple(str(item).strip() for item in configured if str(item).strip())
    return DEFAULT_POLICY_ENTITIES


def policy_url() -> str:
    return (
        getattr(settings, "AIDL_POLICY_URL", None)
        or "https://www.spinifexit.com/acceptable-use-policy"
    ).strip()


def logo_url() -> str:
    # Prefer PNG — Teams Adaptive Cards often fail to load SVG images.
    return (
        getattr(settings, "AIDL_LOGO_URL", None)
        or "https://aidl-backend.onrender.com/static/aidl/logo.png"
    ).strip()


def _tabs_base_url() -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://aidl-backend.onrender.com/api/teams"


def _header_block(org_name: str) -> dict:
    return {
        "type": "ColumnSet",
        "spacing": "None",
        "columns": [
            {
                "type": "Column",
                "width": "auto",
                "items": [
                    {
                        "type": "Image",
                        "url": logo_url(),
                        "size": "Medium",
                        "width": "48px",
                        "height": "48px",
                    }
                ],
                "verticalContentAlignment": "Center",
            },
            {
                "type": "Column",
                "width": "stretch",
                "spacing": "Small",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "AIDL",
                        "weight": "Bolder",
                        "size": "Medium",
                        "spacing": "None",
                    },
                    {
                        "type": "TextBlock",
                        "text": f"for {org_name}",
                        "isSubtle": True,
                        "size": "Small",
                        "spacing": "None",
                        "wrap": True,
                    },
                ],
                "verticalContentAlignment": "Center",
            },
        ],
    }


def _nav_row(active_tab: str) -> dict:
    """Same purple-pill nav strip as the Admin Center card, wrapping to 2
    rows of 2 — each pill deep-links to that tab's own standalone page so the
    strip is still useful even though these 4 cards are sent as separate
    bot/DM messages, not one togglable card."""
    base = _tabs_base_url()
    columns = []
    for tab_id, label, icon in TEAMS_TABS:
        active = tab_id == active_tab
        columns.append(
            {
                "type": "Column",
                "width": "auto",
                "selectAction": {
                    "type": "Action.OpenUrl",
                    "url": f"{base}/tabs/{tab_id}/",
                },
                "items": [
                    {
                        "type": "Container",
                        "style": "emphasis",
                        "spacing": "None",
                        **({"backgroundImage": {"url": _PURPLE_BG, "fillMode": "repeat"}} if active else {}),
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": f"{icon} {label}",
                                "weight": "Bolder",
                                "size": "Small",
                                "color": "light" if active else "default",
                                "wrap": False,
                                "spacing": "None",
                                "horizontalAlignment": "Center",
                            }
                        ],
                    }
                ],
            }
        )
    return {
        "type": "Container",
        "spacing": "Small",
        "items": [
            {"type": "ColumnSet", "spacing": "Small", "columns": columns[:2]},
            {"type": "ColumnSet", "spacing": "Small", "columns": columns[2:]},
        ],
    }


def _pill_button(title: str, url: str) -> dict:
    """A purple pill-shaped call-to-action — native Action.OpenUrl buttons
    render in Teams' own host color, not an arbitrary hex, so this uses the
    same backgroundImage trick as the nav pills to get the exact brand color."""
    return {
        "type": "Container",
        "style": "emphasis",
        "backgroundImage": {"url": _PURPLE_BG, "fillMode": "repeat"},
        "spacing": "Medium",
        "selectAction": {"type": "Action.OpenUrl", "url": url},
        "items": [
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "color": "light",
                "horizontalAlignment": "Center",
                "spacing": "None",
            }
        ],
    }


def _policy_checklist_block() -> dict:
    return {
        "type": "Container",
        "style": "emphasis",
        "bleed": True,
        "items": [
            {
                "type": "TextBlock",
                "text": "Acceptable Use of Technology Policy — the short version",
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "Read the full policy before you start using AI tools at work.",
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            },
        ],
        "spacing": "Medium",
    }


def build_home_card(*, full_name: str = "", org_name: str = "") -> dict:
    """Welcome card shown on Home tab and after Teams signup."""
    org = org_display_name(org_name)
    name = first_name(full_name)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _nav_row("home"),
            _header_block(org),
            {
                "type": "TextBlock",
                "text": f"Welcome to AIDL, {name}! 👋",
                "size": "ExtraLarge",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": (
                    f"You've been added to **{org}'s** AI Driving Licence programme. "
                    "Before we issue your permit, take a second to agree to our "
                    "Acceptable Use Policy."
                ),
                "wrap": True,
                "spacing": "Small",
            },
            _policy_checklist_block(),
            _pill_button("Read full policy", policy_url()),
            {
                "type": "TextBlock",
                "text": f"Sent automatically on signup · {org}",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Medium",
                "wrap": True,
            },
        ],
        "msteams": {
            "width": "Full",
        },
    }


def _level_toggle(active: str) -> dict:
    def _tile(code: str, label: str, is_active: bool) -> dict:
        return {
            "type": "Column",
            "width": "stretch",
            "items": [
                {
                    "type": "Container",
                    "style": "emphasis",
                    **({"backgroundImage": {"url": _YELLOW_BG, "fillMode": "repeat"}} if is_active else {}),
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": code,
                            "weight": "Bolder",
                            "horizontalAlignment": "Center",
                            "spacing": "None",
                            "isSubtle": not is_active,
                        },
                        {
                            "type": "TextBlock",
                            "text": label,
                            "size": "Small",
                            "weight": "Bolder",
                            "horizontalAlignment": "Center",
                            "spacing": "None",
                            "isSubtle": not is_active,
                        },
                    ],
                }
            ],
        }

    return {
        "type": "ColumnSet",
        "spacing": "Medium",
        "columns": [
            _tile("L", "Learner", active == "learner"),
            _tile("F", "Full", active == "full"),
        ],
    }


def _licence_card_visual(*, full_name: str, org_name: str) -> dict:
    name = (full_name or "Jordan Ellis").strip().upper()
    return {
        "type": "Container",
        "style": "emphasis",
        "backgroundImage": {"url": _YELLOW_BG, "fillMode": "repeat"},
        "spacing": "Medium",
        "items": [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "AI DRIVING LICENSE",
                                "weight": "Bolder",
                                "size": "Medium",
                                "spacing": "None",
                            },
                            {
                                "type": "TextBlock",
                                "text": f"ISSUED FOR {org_name.upper()}",
                                "size": "Small",
                                "isSubtle": True,
                                "spacing": "None",
                                "wrap": True,
                            },
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": "L",
                                "weight": "Bolder",
                                "horizontalAlignment": "Center",
                            }
                        ],
                        "style": "default",
                    },
                ],
            },
            {
                "type": "TextBlock",
                "text": name,
                "weight": "Bolder",
                "size": "ExtraLarge",
                "spacing": "Medium",
                "wrap": True,
            },
            {
                "type": "ColumnSet",
                "spacing": "Medium",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {"type": "TextBlock", "text": "CLASS", "size": "Small", "isSubtle": True, "spacing": "None"},
                            {"type": "TextBlock", "text": "Learner's Permit", "weight": "Bolder", "spacing": "None"},
                            {"type": "TextBlock", "text": "EXPIRES", "size": "Small", "isSubtle": True, "spacing": "Medium"},
                            {"type": "TextBlock", "text": "1 year from issue", "weight": "Bolder", "spacing": "None"},
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            {"type": "TextBlock", "text": "ISSUED", "size": "Small", "isSubtle": True, "spacing": "None"},
                            {"type": "TextBlock", "text": "Today", "weight": "Bolder", "spacing": "None"},
                            {"type": "TextBlock", "text": "STATUS", "size": "Small", "isSubtle": True, "spacing": "Medium"},
                            {"type": "TextBlock", "text": "ACTIVE", "weight": "Bolder", "spacing": "None"},
                        ],
                    },
                ],
            },
            {
                "type": "Container",
                "style": "default",
                "spacing": "Medium",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "AIDL-L-" + "".join(str(ord(c) % 10) for c in (name[:4] or "AIDL")),
                        "size": "Small",
                        "isSubtle": True,
                        "spacing": "None",
                    }
                ],
            },
        ],
    }


def build_learners_permit_card(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _nav_row("learners-permit"),
            _header_block(org),
            {
                "type": "TextBlock",
                "text": "Your Learner's Permit is ready",
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            _level_toggle("learner"),
            _licence_card_visual(full_name=full_name, org_name=org),
            {
                "type": "TextBlock",
                "text": (
                    "This is your AI Driving Licence. You're currently at **Level L — "
                    "Learner**. As you complete lessons and pass checks, you'll move up "
                    "to higher levels."
                ),
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "Share: 𝕏 · LinkedIn · Facebook · WhatsApp",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Small",
                "wrap": True,
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Download License",
                "url": f"{_tabs_base_url()}/cards/learners-permit/",
            }
        ],
        "msteams": {"width": "Full"},
    }


_HIGHWAY_CODE_ROWS = (
    (
        "STOP", "attention", "Keep Private Things Private",
        "Never paste **passwords, ID numbers, bank details, or your home "
        "address** into a consumer AI tool. Once it's in, you've lost control of it.",
    ),
    (
        "CHECK", "warning", "Check Before You Trust",
        "AI can state wrong things confidently. **Verify facts, dates, and "
        "numbers** against a real source before you rely on them.",
    ),
    (
        "YOU", "warning", "You're Still the Driver",
        "AI drafts; you decide. **Read and edit every output** and make it "
        "your own before you use or send it.",
    ),
    (
        "ASK", "accent", "Better Prompt, Better Answer",
        "Vague questions get vague answers. **Say who the AI should be, what "
        "you want, and how it should look** — that's the PREP habit.",
    ),
    (
        "ONE WAY", "default", "Mind What You Share",
        "Free tools may learn from what you type. **Treat every prompt like "
        "a postcard** — assume it could be read.",
    ),
)

_TRAFFIC_LIGHT_SECTIONS = (
    (
        "good", "🟢 GO — Public, non-personal.",
        ["General questions & explanations", "Public articles to summarise", "Story, recipe, and idea prompts"],
        "Any tool you like — then check facts.",
    ),
    (
        "warning", "🟡 CAUTION — A little personal.",
        ["Your first name or city", "Your rough plans or preferences", "Non-sensitive everyday details"],
        "Use a placeholder or remove it first.",
    ),
    (
        "attention", "🔴 STOP — Private, keep it out.",
        ["Passwords, PINs, verification codes", "Bank/card numbers, national ID", "Home address, other people's data"],
        "Never paste. Anonymise, then retry.",
    ),
)


def _highway_code_row(icon: str, style: str, title: str, body: str) -> dict:
    return {
        "type": "ColumnSet",
        "spacing": "Medium",
        "columns": [
            {
                "type": "Column",
                "width": "60px",
                "items": [
                    {
                        "type": "Container",
                        "style": style,
                        "spacing": "None",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": icon,
                                "weight": "Bolder",
                                "size": "Small",
                                "horizontalAlignment": "Center",
                                "spacing": "None",
                                "wrap": True,
                            }
                        ],
                    }
                ],
                "verticalContentAlignment": "Center",
            },
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True, "spacing": "None"},
                    {"type": "TextBlock", "text": body, "wrap": True, "spacing": "None", "size": "Small"},
                ],
            },
        ],
    }


def build_highway_code_card(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    rows = [_highway_code_row(*row) for row in _HIGHWAY_CODE_ROWS]
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _nav_row("highway-code"),
            _header_block(org),
            {
                "type": "TextBlock",
                "text": "The Highway Code",
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": (
                    "The everyday rules for using AI safely and confidently. Learn "
                    "them, follow them, drive happy."
                ),
                "wrap": True,
            },
            *rows,
            _pill_button("Open full Highway Code", policy_url()),
        ],
        "msteams": {"width": "Full"},
    }


def _traffic_light_section(style: str, title: str, bullets: list[str], action_text: str) -> dict:
    return {
        "type": "Container",
        "style": style,
        "spacing": "Medium",
        "items": [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True, "spacing": "None"},
            {
                "type": "TextBlock",
                "text": "\n".join(f"• {b}" for b in bullets),
                "wrap": True,
                "spacing": "Small",
            },
            {
                "type": "TextBlock",
                "text": f"→ {action_text}",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Small",
                "size": "Small",
            },
        ],
    }


def build_traffic_light_check_card(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            _nav_row("traffic-light-check"),
            _header_block(org),
            {
                "type": "TextBlock",
                "text": "🚦 Traffic Light Check",
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "Before you paste anything into an AI, check the lights.",
                "wrap": True,
            },
            *[_traffic_light_section(*section) for section in _TRAFFIC_LIGHT_SECTIONS],
            {
                "type": "TextBlock",
                "text": "Was this card useful? 👍 128 · 👎 6",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Medium",
                "wrap": True,
            },
        ],
        "msteams": {"width": "Full"},
    }


CARD_BUILDERS = {
    "home": build_home_card,
    "learners-permit": build_learners_permit_card,
    "highway-code": build_highway_code_card,
    "traffic-light-check": build_traffic_light_check_card,
}


def build_card(tab: str, *, full_name: str = "", org_name: str = "") -> dict | None:
    builder = CARD_BUILDERS.get(tab)
    if not builder:
        return None
    return builder(full_name=full_name, org_name=org_name)


# --- Plain JSON content (no Adaptive Card scaffolding) -----------------
#
# Used by the Learner tab HTML page (templates/teams/tab.html), which
# renders these fields directly into the DOM via fetch(), instead of
# parsing them as an Adaptive Card. The Adaptive Card builders above stay —
# they're still needed to post the same content as a bot message into the
# Teams channel (send_channel_adaptive_card only accepts Adaptive Card JSON).


def build_home_content(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    name = first_name(full_name)
    return {
        "tab": "home",
        "org": org,
        "title": f"Welcome to AIDL, {name}! 👋",
        "body": (
            f"You've been added to **{org}'s** AI Driving Licence programme. "
            "Before we issue your permit, take a second to agree to our "
            "Acceptable Use Policy."
        ),
        "policy_note_title": "Acceptable Use of Technology Policy — the short version",
        "policy_note_body": "Read the full policy before you start using AI tools at work.",
        "policy_url": policy_url(),
        "footer": f"Sent automatically on signup · {org}",
    }


def build_learners_permit_content(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    name = (full_name or "Jordan Ellis").strip().upper()
    card_number = "AIDL-L-" + "".join(str(ord(c) % 10) for c in (name[:4] or "AIDL"))
    return {
        "tab": "learners-permit",
        "org": org,
        "title": "Your Learner's Permit is ready",
        "level": "learner",
        "name": name,
        "class_label": "Learner's Permit",
        "expires": "1 year from issue",
        "issued": "Today",
        "status": "ACTIVE",
        "card_number": card_number,
        "body": (
            "This is your AI Driving Licence. You're currently at **Level L — "
            "Learner**. As you complete lessons and pass checks, you'll move up "
            "to higher levels."
        ),
        "share_note": "Share: 𝕏 · LinkedIn · Facebook · WhatsApp",
    }


def build_highway_code_content(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    return {
        "tab": "highway-code",
        "org": org,
        "title": "The Highway Code",
        "body": (
            "The everyday rules for using AI safely and confidently. Learn "
            "them, follow them, drive happy."
        ),
        "rows": [
            {"icon": icon, "style": style, "title": title, "body": body}
            for icon, style, title, body in _HIGHWAY_CODE_ROWS
        ],
        "policy_url": policy_url(),
    }


def build_traffic_light_check_content(*, full_name: str = "", org_name: str = "") -> dict:
    org = org_display_name(org_name)
    return {
        "tab": "traffic-light-check",
        "org": org,
        "title": "🚦 Traffic Light Check",
        "body": "Before you paste anything into an AI, check the lights.",
        "sections": [
            {"style": style, "title": title, "bullets": bullets, "action": action}
            for style, title, bullets, action in _TRAFFIC_LIGHT_SECTIONS
        ],
        "feedback": "Was this card useful? 👍 128 · 👎 6",
    }


CONTENT_BUILDERS = {
    "home": build_home_content,
    "learners-permit": build_learners_permit_content,
    "highway-code": build_highway_code_content,
    "traffic-light-check": build_traffic_light_check_content,
}


def build_tab_content(tab: str, *, full_name: str = "", org_name: str = "") -> dict | None:
    builder = CONTENT_BUILDERS.get(tab)
    if not builder:
        return None
    return builder(full_name=full_name, org_name=org_name)
