"""Adaptive Cards for AIDL Admin Center — rendered inside Teams Posts (VaptFix-style)."""

from __future__ import annotations

from django.conf import settings

from .cards_catalog_data import CARD_CATALOG
from .org_service import build_admin_dashboard_from_db, build_admin_tab_payload
from .teams_admin import ADMIN_TABS
from .teams_cards import logo_url

# The combined welcome card below packs every tab's full section into ONE
# Adaptive Card message, which Graph caps at ~28KB — measured at ~27.7KB
# for a typical org with all 7 ADMIN_TABS, leaving almost no headroom before
# Graph starts rejecting the post outright (Posts then falls back to the
# bare "could not be posted" message). Add Admin and Add User share a single
# pill + section in THIS card only, to keep the nav pill count (and its
# O(n^2) ToggleVisibility target lists) at 6 instead of 7. Both remain full,
# separate tabs everywhere else — the Website Tab bar, admin.html, and the
# JSON API — none of which have this size ceiling.
_WELCOME_CARD_TABS = tuple(t for t in ADMIN_TABS if t[0] != "add-user")


# 1x1 Teams-blue (#5b5fc7 — same accent used across the tab UI) PNG, tiled
# via backgroundImage, to give the active nav pill one consistent highlight
# colour across all six tabs instead of Adaptive Cards' themed "accent"
# style — which Teams renders as its own brand blue/purple, not a color this
# card can otherwise override (Container "style" is a fixed host-themed
# enum, not an arbitrary hex).
_ACTIVE_PILL_BG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAA"
    "DElEQVR4nGOIjj8OAAKaAYI+GmpxAAAAAElFTkSuQmCC"
)


def _nav_base_url() -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://aidl-backend.onrender.com/api/teams"


def _header(org_name: str) -> dict:
    """Brand row with AIDL PNG logo (Teams does not reliably load SVG)."""
    org_name = _clip(org_name, 60)
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


def _nav_actions(active_tab: str, *, email: str = "", org_id: str = "") -> list[dict]:
    """Pill-style nav — Action.Execute needs the AIDL bot (Phase 2)."""
    actions = []
    for tab_id, label, _icon in ADMIN_TABS:
        action = {
            "type": "Action.Execute",
            "title": label,
            "verb": "aidl.nav",
            "data": {
                "action": "nav",
                "tab": tab_id,
                "email": email,
                "organization_id": org_id,
            },
        }
        if tab_id == active_tab:
            action["style"] = "positive"
        actions.append(action)
    return actions


def _status_badge(status: str) -> dict:
    status = (status or "").strip().lower()
    label, color = {
        "approved": ("APPROVED", "good"),
        "pending": ("PENDING", "warning"),
        "rejected": ("PROHIBITED", "attention"),
    }.get(status, (status.upper() or "UNKNOWN", "default"))
    badge: dict = {
        "type": "Container",
        "spacing": "None",
        "items": [
            {
                "type": "TextBlock",
                "text": label,
                "size": "Small",
                "weight": "Bolder",
                "wrap": False,
                "horizontalAlignment": "Center",
            }
        ],
    }
    if color != "default":
        badge["style"] = color
    return badge


def _clip(text: str, limit: int = 90) -> str:
    """Defend the combined card's size cap against free-text org data (an
    admin can type an arbitrarily long app description) — item counts are
    capped elsewhere, but a single very long field could still do damage."""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _item_row(title: str, meta: str, desc: str, *, badge: dict | None = None) -> dict:
    # One TextBlock (markdown bold + newline) instead of up to three — this
    # renders per row of every list in the combined card, so trimming it
    # matters for staying under Graph's ~28KB Adaptive Card size cap.
    lines = [f"**{_clip(title, 60)}**"]
    sub = " · ".join(_clip(x, 90) for x in [meta, desc] if x)
    if sub:
        lines.append(sub)
    columns = [
        {
            "type": "Column",
            "width": "stretch",
            "items": [{"type": "TextBlock", "text": "\n\n".join(lines), "wrap": True, "spacing": "None"}],
        }
    ]
    if badge:
        columns.append({"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [badge]})
    return {
        "type": "Container",
        "style": "emphasis",
        "spacing": "Small",
        "items": [{"type": "ColumnSet", "columns": columns}],
    }


# Hard cap on rows rendered inline per list in the combined (Graph) card —
# admin/policy/app-registry lists grow with the organisation (more seats,
# more registered apps) with no upper bound, unlike CARD_CATALOG below, so
# a fixed slice (unlike items[:12]) is the only way to guarantee this card
# never crosses Graph's ~28KB size cap regardless of how large the org gets.
_INLINE_LIST_CAP = 4


def _overflow_note(tab: str, total: int, shown: int) -> dict | None:
    remaining = total - shown
    if remaining <= 0:
        return None
    return {
        "type": "TextBlock",
        "text": f"+{remaining} more — open the [{tab.replace('-', ' ').title()} tab]({_nav_base_url()}/tabs/{tab}/) to see the full list.",
        "size": "Small",
        "isSubtle": True,
        "wrap": True,
        "spacing": "Small",
    }


def _heading_block(payload: dict, tab: str, subtitle: str = "") -> list[dict]:
    blocks = [
        {
            "type": "TextBlock",
            "text": subtitle,
            "isSubtle": True,
            "size": "Small",
            "spacing": "None",
        }
    ] if subtitle else []
    blocks.append(
        {
            "type": "TextBlock",
            "text": _clip(payload.get("heading") or payload.get("title") or tab, 80),
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Small" if subtitle else "None",
        }
    )
    if payload.get("body"):
        blocks.append({"type": "TextBlock", "text": _clip(payload["body"], 220), "wrap": True, "spacing": "Small"})
    return blocks


def _add_admin_blocks(payload: dict, user=None) -> list[dict]:
    admin_count = payload.get("admin_count") or 0
    admin_seat_limit = payload.get("admin_seat_limit") or 0
    email = payload.get("email") or "name@company.com"

    # Each tile gets its own Adaptive Card container style so the three
    # permissions read as distinct colored chips instead of one highlighted
    # tile next to two blank ones.
    permission_tiles = [
        ("Approve Apps", "Governance", "warning"),
        ("Access Cards", "Reference cards", "accent"),
        ("Create Card", "Add new cards", "good"),
    ]

    def _tile(title: str, sub: str, style: str) -> dict:
        return {
            "type": "Container",
            "style": style,
            "spacing": "Small",
            "items": [
                {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True, "spacing": "None"},
                {"type": "TextBlock", "text": sub, "size": "Small", "isSubtle": True, "wrap": True, "spacing": "None"},
            ],
        }

    return [
        *_heading_block(payload, "add-admin", "Optional, whenever you need a hand · Admin Team · Add more admins"),
        {"type": "TextBlock", "text": "PERMISSIONS", "size": "Small", "weight": "Bolder", "isSubtle": True, "spacing": "Medium"},
        {
            "type": "ColumnSet",
            "spacing": "Small",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [_tile(*permission_tiles[0])]},
                {"type": "Column", "width": "stretch", "items": [_tile(*permission_tiles[1])]},
            ],
        },
        {
            "type": "ColumnSet",
            "spacing": "Small",
            "columns": [{"type": "Column", "width": "stretch", "items": [_tile(*permission_tiles[2])]}],
        },
        {
            "type": "ActionSet",
            "spacing": "Medium",
            "actions": [
                # This card is posted via Graph (not the bot), so it has no
                # bot conversation for any invoke-based action (task/fetch,
                # Action.Execute) to reach — those all fail with a generic
                # Teams error. A plain webpage link is the only thing this
                # variant can reliably do; it's just the degrade path for
                # orgs the bot hasn't (re)posted an interactive card for yet
                # (see send_admin_center_card in teams_messaging.py) — once
                # the bot has posted, this whole card is replaced by the
                # fully in-place interactive one (_interactive_add_admin_blocks).
                {
                    "type": "Action.OpenUrl",
                    "title": "Send Admin Invite",
                    "style": "positive",
                    "url": f"{_nav_base_url()}/tabs/add-admin/?email={email}",
                },
                # Add User has its own tab in the Website Tab bar and
                # admin.html, but not its own pill in this combined card (see
                # _WELCOME_CARD_TABS) — this keeps it one tap away anyway.
                {
                    "type": "Action.OpenUrl",
                    "title": "Add a Team Member instead",
                    "url": f"{_nav_base_url()}/tabs/add-user/?email={email}",
                },
            ],
        },
        {
            "type": "TextBlock",
            "text": f"{admin_count} of {admin_seat_limit} admin seats used · {email}",
            "size": "Small",
            "isSubtle": True,
            "spacing": "Small",
            "wrap": True,
        },
    ]


def _add_user_blocks(payload: dict) -> list[dict]:
    licences_issued = payload.get("licences_issued") or 0
    seats_purchased = payload.get("seats_purchased") or 0
    email = payload.get("email") or "name@company.com"

    return [
        *_heading_block(payload, "add-user", "Whenever a new team member joins · Team · Issue a licence"),
        {
            "type": "ActionSet",
            "spacing": "Medium",
            "actions": [
                # Degrade path only — see _add_admin_blocks above.
                {
                    "type": "Action.OpenUrl",
                    "title": "Issue Licence",
                    "style": "positive",
                    "url": f"{_nav_base_url()}/tabs/add-user/?email={email}",
                }
            ],
        },
        {
            "type": "TextBlock",
            "text": f"{licences_issued} of {seats_purchased} licences issued · {email}",
            "size": "Small",
            "isSubtle": True,
            "spacing": "Small",
            "wrap": True,
        },
    ]


def _policy_blocks(payload: dict) -> list[dict]:
    policy_url = payload.get("policy_url") or ""
    signed = payload.get("signed_count") or 0
    unsigned = payload.get("unsigned_count") or 0

    return [
        *_heading_block(
            payload,
            "policy",
            payload.get("body")
            or "The AUP policy is uploaded by your organisation and shown here for every team member to sign.",
        ),
        # Colored "live" status bar — style "accent" instead of a plain
        # unstyled Container so the current policy stands out the way the
        # rest of the highlighted sections (badges, tiles) already do.
        {
            "type": "Container",
            "style": "accent",
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
                                    "text": "CURRENT LIVE POLICY",
                                    "size": "Small",
                                    "weight": "Bolder",
                                    "spacing": "None",
                                }
                            ],
                        },
                        {
                            "type": "Column",
                            "width": "auto",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": "● LIVE",
                                    "size": "Small",
                                    "weight": "Bolder",
                                    "color": "good",
                                    "spacing": "None",
                                }
                            ],
                        },
                    ],
                },
            ],
        },
        # Signed/unsigned as colored stat tiles instead of one subtle
        # footer line — makes the split legible at a glance.
        {
            "type": "ColumnSet",
            "spacing": "Medium",
            "columns": [
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {"type": "TextBlock", "text": "SIGNED", "size": "Small", "isSubtle": True, "spacing": "None"},
                        {
                            "type": "TextBlock",
                            "text": str(signed),
                            "size": "ExtraLarge",
                            "weight": "Bolder",
                            "color": "good",
                            "spacing": "None",
                        },
                    ],
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {"type": "TextBlock", "text": "UNSIGNED", "size": "Small", "isSubtle": True, "spacing": "None"},
                        {
                            "type": "TextBlock",
                            "text": str(unsigned),
                            "size": "ExtraLarge",
                            "weight": "Bolder",
                            "color": "attention" if unsigned else "good",
                            "spacing": "None",
                        },
                    ],
                },
            ],
        },
        {
            "type": "ActionSet",
            "spacing": "Medium",
            "actions": [
                a
                for a in [
                    (
                        {
                            "type": "Action.OpenUrl",
                            "title": "View Current Policy",
                            "style": "positive",
                            "url": policy_url,
                        }
                        if policy_url
                        else None
                    ),
                ]
                if a
            ],
        },
    ]


# Static approximation only — no pricing/rating/quota data model exists yet,
# and a Graph-posted (bot-less) Adaptive Card can't run a real request/send
# flow. "View" and "Request Card" expand inline (Action.ToggleVisibility)
# instead of opening a floating modal or a new browser tab.
CARD_CATALOG = (
    {
        "id": "c0",
        "icon": "📄",
        "title": "Acceptable Use v3.1",
        "category": "POLICY · SIGNATURE REQUIRED",
        "desc": "Your live AUP with the summary your team actually reads.",
        "price": "",
        "rating": "4.1 (39)",
        "requested": True,
    },
    {
        "id": "c1",
        "icon": "🚦",
        "title": "Traffic Light Check",
        "category": "DECISION AID",
        "desc": "Green, amber or red before you hand a task to AI. The three-second gut check.",
        "price": "$49 · one-time",
        "rating": "4.8 (52)",
        "requested": False,
    },
    {
        "id": "c2",
        "icon": "🗂️",
        "title": "Approved Apps Registry",
        "category": "REGISTRY · 12 ENTRIES",
        "desc": "Which AI and IT tools are approved or prohibited, and for what data.",
        "price": "$59 · one-time",
        "rating": "4.4 (27)",
        "requested": False,
    },
    {
        "id": "c3",
        "icon": "⚖️",
        "title": "Data Ethics Gate",
        "category": "GATE BRIEFING",
        "desc": "What blocks a licence: unsigned policy, unlogged agents, unreviewed output.",
        "price": "$39 · one-time",
        "rating": "4.6 (31)",
        "requested": False,
    },
    {
        "id": "c4",
        "icon": "🏛️",
        "title": "Mistake Museum",
        "category": "WORKED EXAMPLES",
        "desc": "Real failures from the team, de-identified, with what should have happened.",
        "price": "$79 · one-time",
        "rating": "4.9 (44)",
        "requested": False,
    },
    {
        "id": "c5",
        "icon": "📋",
        "title": "Data Classification Cheat Sheet",
        "category": "REFERENCE CARD",
        "desc": "Four data classes, four different answers about what you may submit.",
        "price": "$25 · one-time",
        "rating": "4.5 (19)",
        "requested": False,
    },
    {
        "id": "c6",
        "icon": "✅",
        "title": "Redaction Before Submission",
        "category": "CHECKLIST",
        "desc": "Before a document goes into a chat window, strip what the model doesn't need.",
        "price": "$35 · one-time",
        "rating": "4.3 (15)",
        "requested": False,
    },
    {
        "id": "c7",
        "icon": "💬",
        "title": "Customer Data in Prompts",
        "category": "GATE BRIEFING",
        "desc": "Customer data in a prompt is where the conversation stops.",
        "price": "$69 · one-time",
        "rating": "4.7 (22)",
        "requested": False,
    },
    {
        "id": "c8",
        "icon": "✅",
        "title": "The Verification Pass",
        "category": "CHECKLIST",
        "desc": "Three checks before AI output leaves your desk. Every time.",
        "price": "$45 · one-time",
        "rating": "4.4 (17)",
        "requested": False,
    },
    {
        "id": "c9",
        "icon": "📝",
        "title": "Reporting an AI Mistake",
        "category": "TEMPLATE",
        "desc": "When something goes wrong, the first hour matters more than the blame.",
        "price": "$19 · one-time",
        "rating": "4.2 (11)",
        "requested": False,
    },
)

def _card_row(card: dict) -> list[dict]:
    # Wrapped in the same "emphasis" Container as _item_row (used by the
    # other tabs) so each card gets its own subtle background box — without
    # it, consecutive TextBlocks render as one continuous stream of text
    # with no visual break between cards.
    status = card.get("status") or "not_sent"
    status_prefix = "✓ · " if status in ("sent", "scheduled", "requested") else ""
    meta = f"{card['category']} · {status_prefix}★{card['rating'].split(' ')[0]}"
    if card["price"]:
        meta = f"{meta} · {card['price']}"
    return [
        {
            "type": "Container",
            "style": "emphasis",
            "spacing": "Small",
            "items": [
                {
                    "type": "TextBlock",
                    "text": f"**{card['icon']} {card['title']}**\n\n{meta}",
                    "wrap": True,
                    "spacing": "None",
                }
            ],
        },
    ]


def _cards_blocks(payload: dict) -> list[dict]:
    catalog = payload.get("catalog") or CARD_CATALOG
    quota_used = payload.get("quota_used", 0)
    quota_max = payload.get("quota_max", 10)
    blocks: list[dict] = [
        *_heading_block(
            payload,
            "cards",
            "Pick the reference cards your team needs — request or send one from the Cards tab.",
        ),
        {
            "type": "Container",
            "style": "warning",
            "spacing": "Medium",
            "items": [
                {
                    "type": "TextBlock",
                    "text": "MONTHLY CARD QUOTA",
                    "size": "Small",
                    "weight": "Bolder",
                    "spacing": "None",
                },
                {
                    "type": "TextBlock",
                    "text": f"{quota_used} of {quota_max} cards requested this month",
                    "weight": "Bolder",
                    "size": "Medium",
                    "spacing": "Small",
                },
            ],
        },
    ]
    shown_cards = catalog[:_INLINE_LIST_CAP]
    for card in shown_cards:
        blocks.extend(_card_row(card))
    overflow = _overflow_note("cards", len(catalog), len(shown_cards))
    if overflow:
        blocks.append(overflow)
    blocks.append(
        {
            "type": "TextBlock",
            "text": "Need something that's not listed? Request a brand-new reference card from the Cards tab.",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
            "spacing": "Medium",
        }
    )
    return blocks


def _app_list_blocks(tab: str, payload: dict) -> list[dict]:
    items = payload.get("items") or []
    shown = items[:_INLINE_LIST_CAP]
    rows = [
        _item_row(it.get("name") or "App", "", it.get("description") or "", badge=_status_badge(it.get("status") or ""))
        for it in shown
    ] or [{"type": "TextBlock", "text": "No applications registered yet.", "isSubtle": True, "wrap": True}]
    overflow = _overflow_note(tab, len(items), len(shown))
    return [*_heading_block(payload, tab), *rows, *([overflow] if overflow else [])]


def _generic_blocks(tab: str, payload: dict) -> list[dict]:
    items = payload.get("items") or []
    shown = items[:_INLINE_LIST_CAP]
    rows = [
        _item_row(
            it.get("name") or it.get("email") or "Item",
            " · ".join(str(x) for x in [it.get("email"), it.get("role"), it.get("status")] if x),
            it.get("description") or "",
        )
        for it in shown
    ] or [{"type": "TextBlock", "text": "No records yet for this organisation.", "isSubtle": True, "wrap": True}]
    overflow = _overflow_note(tab, len(items), len(shown))
    return [*_heading_block(payload, tab), *rows, *([overflow] if overflow else [])]


def _section_body_blocks(
    tab: str,
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
) -> list[dict]:
    """Tab-specific heading + content, with no header/nav — fills a
    toggle-visibility Container in the combined card (see _combined_card) so
    a pill click swaps this content in below the nav without a bot
    round-trip or a new browser tab."""
    payload = build_admin_tab_payload(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    if tab == "add-admin":
        return _add_admin_blocks(payload, user=user)
    if tab == "add-user":
        return _add_user_blocks(payload)
    if tab == "policy":
        return _policy_blocks(payload)
    if tab == "cards":
        return _cards_blocks(payload)
    if tab in ("ai-apps", "it-apps"):
        return _app_list_blocks(tab, payload)
    return _generic_blocks(tab, payload)


_TAB_INDEX = {t: i for i, (t, _l, _i) in enumerate(_WELCOME_CARD_TABS)}


def _section_element_id(tab_id: str) -> str:
    # Short numeric ids, not the tab name — every id here gets repeated
    # ~18x across the nav's toggle-target lists, so a few bytes per id adds
    # up fast against the ~28KB Adaptive Card size cap.
    return f"s{_TAB_INDEX[tab_id]}"


def _pill_id(tab_id: str, *, active: bool) -> str:
    return f"{'pa' if active else 'pi'}{_TAB_INDEX[tab_id]}"


def _nav_toggle_targets(clicked_tab: str) -> list[dict]:
    """Every element a pill click must force to a specific state: the clicked
    tab's section + coloured pill show, every other section + coloured pill
    hide (and every other plain pill comes back)."""
    targets: list[dict] = []
    for tab_id, _label, _icon in _WELCOME_CARD_TABS:
        targets.append({"elementId": _section_element_id(tab_id), "isVisible": tab_id == clicked_tab})
        targets.append({"elementId": _pill_id(tab_id, active=True), "isVisible": tab_id == clicked_tab})
        targets.append({"elementId": _pill_id(tab_id, active=False), "isVisible": tab_id != clicked_tab})
    return targets


def _nav_pill_rows(active_tab: str) -> dict:
    """
    In-Teams clickable nav, no bot, no browser tab — each tab is TWO
    overlapping pills (a coloured "active" one, a plain "inactive" one, only
    one ever isVisible), both wired to the same Action.ToggleVisibility.
    Clicking one force-shows its own section + its own coloured pill and
    force-hides every other section and every other coloured pill, so the
    highlight colour actually moves to whichever pill was just clicked and
    the previous section's content actually disappears — a real tab strip
    entirely client-side, inside the one posted message.
    """
    columns = []
    for tab_id, label, icon in _WELCOME_CARD_TABS:
        title = f"{icon} {label}" if icon else label
        toggle_action = {
            "type": "Action.ToggleVisibility",
            "targetElements": _nav_toggle_targets(tab_id),
        }
        columns.append(
            {
                "type": "Column",
                # "auto" — a fixed px width clipped longer labels like
                # "Add Admin"/"AI Apps"; smaller font + no extra spacing
                # (below) already keeps the pills compact without that.
                "width": "auto",
                "selectAction": toggle_action,
                "items": [
                    {
                        "type": "Container",
                        "id": _pill_id(tab_id, active=True),
                        "isVisible": tab_id == active_tab,
                        "style": "emphasis",
                        "backgroundImage": {"url": _ACTIVE_PILL_BG, "fillMode": "repeat"},
                        "spacing": "None",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": title,
                                "weight": "Bolder",
                                "size": "Small",
                                "color": "light",
                                "wrap": False,
                                "spacing": "None",
                                "horizontalAlignment": "Center",
                            }
                        ],
                    },
                    {
                        "type": "Container",
                        "id": _pill_id(tab_id, active=False),
                        "isVisible": tab_id != active_tab,
                        "style": "emphasis",
                        "spacing": "None",
                        "items": [
                            {
                                "type": "TextBlock",
                                "text": title,
                                "weight": "Bolder",
                                "size": "Small",
                                "wrap": False,
                                "spacing": "None",
                                "horizontalAlignment": "Center",
                            }
                        ],
                    },
                ],
            }
        )

    # Two rows so the six pills wrap the same way as the reference design.
    # Every pill (active or not) carries a "style" so Teams always draws a
    # visible pill box — a Container with no style renders as bare text with
    # no border/background at all, which is why only the active tab used to
    # look like a button and the rest read as plain inline words.
    return {
        "type": "Container",
        "spacing": "Small",
        "items": [
            {"type": "ColumnSet", "spacing": "Small", "columns": columns[:4]},
            {"type": "ColumnSet", "spacing": "Small", "columns": columns[4:]},
        ],
    }


def _nav_container(
    active_tab: str,
    *,
    email: str = "",
    org_id: str = "",
    interactive: bool = False,
) -> dict:
    if interactive:
        return {
            "type": "ActionSet",
            "actions": _nav_actions(active_tab, email=email, org_id=org_id),
        }
    return _nav_pill_rows(active_tab)


def _stat_tile(label: str, value: str, sub: str, *, alert: bool = False, style: str = "emphasis") -> dict:
    return {
        "type": "Container",
        "style": "attention" if alert else style,
        "items": [
            {
                "type": "TextBlock",
                "text": label,
                "size": "Small",
                "weight": "Bolder",
                "isSubtle": True,
                "spacing": "None",
            },
            {
                "type": "TextBlock",
                "text": value,
                "size": "ExtraLarge",
                "weight": "Bolder",
                "color": "attention" if alert else "default",
                "spacing": "Small",
            },
            {
                "type": "TextBlock",
                "text": sub,
                "size": "Small",
                "isSubtle": True,
                "spacing": "None",
                "wrap": True,
            },
        ],
    }


def build_admin_adaptive_card(
    tab: str = "home",
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
    interactive: bool = False,
) -> dict:
    """
    Build Admin Center Adaptive Card (dynamic from DB).

    interactive=False (default for Graph channel posts): a single combined
    card with every tab's section pre-built and Action.ToggleVisibility nav —
    no Action.Execute, since Graph user posts reject / ignore bot verbs
    (which can leave Posts empty), but still a real in-place tab switch (old
    section hides the moment another one is opened) with no bot and no
    browser tab. Kept lean (CARD_CATALOG, per-card markup) to stay under
    Graph's ~28KB Adaptive Card limit — over it, Graph rejects the card
    outright and Posts shows nothing.
    interactive=True: bot-driven in-place nav (Phase 2) — one section per
    message, replaced wholesale by the bot on each click.
    """
    tab = (tab or "home").strip().lower()
    if not interactive:
        return _combined_card(
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
            active_tab=tab,
        )
    if tab == "home":
        return _home_card(
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
            interactive=interactive,
        )
    return _section_card(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
        interactive=interactive,
    )


# Style cycles that give the stat/governance tiles visual variety within
# Adaptive Cards' fixed style enum (no arbitrary hex per tile) — chosen for
# what each metric actually means, not just alternated for looks: "good" on
# the approved count, "accent" as the brand highlight, "emphasis" as neutral.
_STAT_STYLES = ("accent", "emphasis", "emphasis")
_GOV_STYLES = ("emphasis", "good", "accent", "accent")


def _home_body_items(dash: dict) -> list[dict]:
    """Depot Overview + welcome + stats + governance + progress — Home's own
    section content, with no header/nav (those are shared, above the tabs)."""
    stats = dash.get("stats") or []
    stats_cols = []
    for idx, s in enumerate(stats[:3]):
        stats_cols.append(
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    _stat_tile(
                        s.get("label", ""),
                        s.get("value", "0"),
                        s.get("sub", ""),
                        alert=bool(s.get("alert")),
                        style=_STAT_STYLES[idx % len(_STAT_STYLES)],
                    )
                ],
            }
        )

    gov = dash.get("governance") or []
    gov_row_1 = []
    gov_row_2 = []
    for idx, g in enumerate(gov[:4]):
        col = {
            "type": "Column",
            "width": "stretch",
            "items": [
                _stat_tile(
                    g.get("label", ""),
                    g.get("value", "0"),
                    g.get("sub", ""),
                    style=_GOV_STYLES[idx % len(_GOV_STYLES)],
                )
            ],
        }
        if idx < 2:
            gov_row_1.append(col)
        else:
            gov_row_2.append(col)

    items: list[dict] = [
        {
            "type": "TextBlock",
            "text": "Depot Overview",
            "isSubtle": True,
            "size": "Small",
            "spacing": "None",
        },
        {
            "type": "Container",
            "spacing": "Medium",
            "items": [
                {
                    "type": "TextBlock",
                    "text": _clip(dash["welcome_title"], 120),
                    "size": "Large",
                    "weight": "Bolder",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": _clip(dash["welcome_body"], 220),
                    "wrap": True,
                    "spacing": "Small",
                },
            ],
        },
    ]

    if stats_cols:
        items.append({"type": "ColumnSet", "spacing": "Medium", "columns": stats_cols})

    items.append(
        {
            "type": "TextBlock",
            "text": "GOVERNANCE SNAPSHOT",
            "size": "Small",
            "weight": "Bolder",
            "isSubtle": True,
            "spacing": "Large",
        }
    )
    if gov_row_1:
        items.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_1})
    if gov_row_2:
        items.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_2})

    items.append(
        {
            "type": "TextBlock",
            "text": _clip(dash["progress_text"], 140),
            "size": "Small",
            "isSubtle": True,
            "spacing": "Medium",
            "wrap": True,
        }
    )
    return items


def _combined_card(
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
    active_tab: str = "home",
) -> dict:
    """
    One AdaptiveCard holding every tab's section as a Container keyed
    "s<index>", with only the active one visible at a time. Nav pills use
    Action.ToggleVisibility to force-show the clicked section and
    force-hide every other one, so switching tabs behaves like a real tab
    strip, entirely inside the one posted message — no bot, no browser tab.
    """
    active_tab = (active_tab or "home").strip().lower()
    if active_tab not in _TAB_INDEX:
        # e.g. "add-user" — has no pill in this size-capped card (see
        # _WELCOME_CARD_TABS); land on Home instead of showing no section.
        active_tab = "home"
    dash = build_admin_dashboard_from_db(
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = dash.get("organization_id") or ""
    org = dash.get("org_name") or org_name or "AIDL"

    sections = [
        {
            "type": "Container",
            "id": _section_element_id("home"),
            "isVisible": active_tab == "home",
            "items": _home_body_items(dash),
        }
    ]
    for tab_id, _label, _icon in _WELCOME_CARD_TABS:
        if tab_id == "home":
            continue
        sections.append(
            {
                "type": "Container",
                "id": _section_element_id(tab_id),
                "isVisible": active_tab == tab_id,
                "items": _section_body_blocks(
                    tab_id,
                    full_name=full_name,
                    org_name=org_name,
                    email=email,
                    user=user,
                ),
            }
        )

    body = [
        _header(org),
        {
            "type": "TextBlock",
            "text": "Admin Center",
            "size": "Medium",
            "weight": "Bolder",
            "spacing": "Medium",
        },
        _nav_container(active_tab, email=email, org_id=org_id, interactive=False),
        *sections,
    ]

    export_url = (
        "https://aidl-backend.onrender.com/api/teams/admin/export/"
        f"?email={email}"
        if email
        else "https://aidl-backend.onrender.com/api/teams/admin/export/"
    )

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Export Coverage CSV",
                "url": export_url,
            },
        ],
        "msteams": {"width": "Full"},
    }


def _home_card(
    *,
    full_name: str,
    org_name: str,
    email: str,
    user,
    interactive: bool = False,
) -> dict:
    dash = build_admin_dashboard_from_db(
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = dash.get("organization_id") or ""
    org = dash.get("org_name") or org_name or "AIDL"

    stats = dash.get("stats") or []
    stats_cols = []
    for idx, s in enumerate(stats[:3]):
        stats_cols.append(
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    _stat_tile(
                        s.get("label", ""),
                        s.get("value", "0"),
                        s.get("sub", ""),
                        alert=bool(s.get("alert")),
                        style=_STAT_STYLES[idx % len(_STAT_STYLES)],
                    )
                ],
            }
        )

    gov = dash.get("governance") or []
    gov_row_1 = []
    gov_row_2 = []
    for idx, g in enumerate(gov[:4]):
        col = {
            "type": "Column",
            "width": "stretch",
            "items": [
                _stat_tile(
                    g.get("label", ""),
                    g.get("value", "0"),
                    g.get("sub", ""),
                    style=_GOV_STYLES[idx % len(_GOV_STYLES)],
                )
            ],
        }
        if idx < 2:
            gov_row_1.append(col)
        else:
            gov_row_2.append(col)

    body = [
        _header(org),
        {
            "type": "TextBlock",
            "text": "Admin Center",
            "size": "Medium",
            "weight": "Bolder",
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": "Depot Overview",
            "isSubtle": True,
            "size": "Small",
            "spacing": "None",
        },
        _nav_container(
            "home",
            email=email,
            org_id=org_id,
            interactive=interactive,
        ),
        {
            "type": "Container",
            "spacing": "Medium",
            "items": [
                {
                    "type": "TextBlock",
                    "text": _clip(dash["welcome_title"], 120),
                    "size": "Large",
                    "weight": "Bolder",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": _clip(dash["welcome_body"], 220),
                    "wrap": True,
                    "spacing": "Small",
                },
            ],
        },
    ]

    if stats_cols:
        body.append(
            {
                "type": "ColumnSet",
                "spacing": "Medium",
                "columns": stats_cols,
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": "GOVERNANCE SNAPSHOT",
            "size": "Small",
            "weight": "Bolder",
            "isSubtle": True,
            "spacing": "Large",
        }
    )
    if gov_row_1:
        body.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_1})
    if gov_row_2:
        body.append({"type": "ColumnSet", "spacing": "Small", "columns": gov_row_2})

    body.append(
        {
            "type": "TextBlock",
            "text": _clip(dash["progress_text"], 140),
            "size": "Small",
            "isSubtle": True,
            "spacing": "Medium",
            "wrap": True,
        }
    )

    export_url = (
        "https://aidl-backend.onrender.com/api/teams/admin/export/"
        f"?email={email}"
        if email
        else "https://aidl-backend.onrender.com/api/teams/admin/export/"
    )

    actions: list[dict] = [
        {
            "type": "Action.OpenUrl",
            "title": "Export Coverage CSV",
            "url": export_url,
        },
    ]
    # _home_card is only reached via the interactive=True (bot) path —
    # interactive=False now goes through _combined_card instead.
    actions.append(
        {
            "type": "Action.Execute",
            "title": "Next: add another admin",
            "verb": "aidl.nav",
            "style": "positive",
            "data": {
                "action": "nav",
                "tab": "add-admin",
                "email": email,
                "organization_id": org_id,
            },
        }
    )

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": actions,
        "msteams": {"width": "Full"},
    }


def _generic_item_blocks(items: list[dict]) -> list[dict]:
    item_blocks = []
    for it in items[:12]:
        title = it.get("name") or it.get("email") or "Item"
        meta = " · ".join(
            str(x) for x in [it.get("email"), it.get("role"), it.get("status")] if x
        )
        desc = it.get("description") or ""
        block_items = [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True, "spacing": "None"}
        ]
        if meta:
            block_items.append(
                {"type": "TextBlock", "text": meta, "size": "Small", "isSubtle": True, "wrap": True, "spacing": "None"}
            )
        if desc:
            block_items.append(
                {"type": "TextBlock", "text": desc, "size": "Small", "wrap": True, "spacing": "None"}
            )
        item_blocks.append({"type": "Container", "style": "emphasis", "items": block_items, "spacing": "Small"})
    if not item_blocks:
        item_blocks.append(
            {"type": "TextBlock", "text": "No records yet for this organisation.", "isSubtle": True, "wrap": True}
        )
    return item_blocks


def _bool_toggle(value: bool = True) -> str:
    # Adaptive Cards Input.Toggle values are strings, not JSON booleans.
    return "true" if value else "false"


def _roster_choice_set(user) -> dict | None:
    """Input.ChoiceSet of the caller's current Teams roster — Teams renders a
    compact ChoiceSet as a searchable dropdown natively, giving the same
    "start typing a name, pick them" picker as VAPTfix's Fetch Details, with
    no extra fetch round trip needed. Choice values encode "email::Full Name"
    (split by _split_roster_pick in teams_bot_views.py). Returns None when
    there's no usable token or the roster is empty, so callers can skip the
    picker and fall back to manual entry only.
    """
    if user is None:
        return None
    from .microsoft_auth import get_access_token_for_user, list_team_members

    access_token = get_access_token_for_user(user)
    if not access_token:
        return None
    members = list_team_members(access_token, user.teams_team_id)
    choices = [
        {
            "title": f"{m['full_name']} ({m['email']})" if m.get("full_name") else m["email"],
            "value": f"{m['email']}::{m.get('full_name', '')}",
        }
        for m in members
        if m.get("email")
    ]
    if not choices:
        return None
    return {
        "type": "Input.ChoiceSet",
        "id": "rosterPick",
        "label": "Pick someone already in this Teams team",
        "style": "compact",
        "placeholder": "Start typing a name…",
        "choices": [{"title": "— fill in manually below —", "value": ""}] + choices,
        "value": "",
    }


def _interactive_add_admin_blocks(payload: dict, user=None) -> tuple[list[dict], list[dict]]:
    """Promote-to-admin form (email + the 3 permission chips as toggles) —
    the bot-native equivalent of the Website Tab's Add Admin screen."""
    items = payload.get("items") or []
    rows = []
    for it in items[:8]:
        title = it.get("name") or it.get("email") or "Admin"
        perms = it.get("permissions") or {}
        on = [
            label
            for label, key in (
                ("Approve Apps", "approve_apps"),
                ("Access Cards", "access_cards"),
                ("Create Card", "create_card"),
            )
            if perms.get(key)
        ]
        rows.append(
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Small",
                "items": [
                    {"type": "TextBlock", "text": f"**{title}**", "wrap": True, "spacing": "None"},
                    {"type": "TextBlock", "text": it.get("email") or "", "size": "Small", "isSubtle": True, "wrap": True, "spacing": "None"},
                    {
                        "type": "TextBlock",
                        "text": ", ".join(on) if on else "No extra permissions",
                        "size": "Small",
                        "isSubtle": True,
                        "wrap": True,
                        "spacing": "None",
                    },
                ],
            }
        )
    body = rows or [{"type": "TextBlock", "text": "No admins yet.", "isSubtle": True, "wrap": True}]
    body.append({"type": "TextBlock", "text": "PROMOTE TO ADMIN", "size": "Small", "weight": "Bolder", "isSubtle": True, "spacing": "Medium"})
    roster = _roster_choice_set(user)
    if roster:
        body.append(roster)
        body.append(
            {
                "type": "TextBlock",
                "text": "— or fill in manually below for someone not yet in this Teams team —",
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        )
    body += [
        {"type": "Input.Text", "id": "promoteEmail", "label": "Work email", "placeholder": "name@company.com"},
        {"type": "Input.Toggle", "id": "permApproveApps", "title": "Approve Apps", "value": _bool_toggle(True)},
        {"type": "Input.Toggle", "id": "permAccessCards", "title": "Access Cards", "value": _bool_toggle(True)},
        {"type": "Input.Toggle", "id": "permCreateCard", "title": "Create Card", "value": _bool_toggle(True)},
    ]
    actions = [
        {
            "type": "Action.Execute",
            "title": "Send Admin Invite",
            "verb": "aidl.promote_admin",
            "style": "positive",
            "data": {"action": "promote_admin", "tab": "add-admin"},
        }
    ]
    return body, actions


def _interactive_add_user_blocks(payload: dict, user=None) -> tuple[list[dict], list[dict]]:
    licences_issued = payload.get("licences_issued") or 0
    seats_purchased = payload.get("seats_purchased") or 0
    body = [
        {
            "type": "TextBlock",
            "text": f"{licences_issued} of {seats_purchased} licences issued",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
        },
    ]
    roster = _roster_choice_set(user)
    if roster:
        body.append(roster)
        body.append(
            {
                "type": "TextBlock",
                "text": "— or fill in manually below for someone not yet in this Teams team —",
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        )
    body += [
        {"type": "Input.Text", "id": "inviteName", "label": "Full name", "placeholder": "Full name"},
        {"type": "Input.Text", "id": "inviteEmail", "label": "Work email", "placeholder": "name@company.com"},
    ]
    actions = [
        {
            "type": "Action.Execute",
            "title": "Issue Licence",
            "verb": "aidl.invite_user",
            "style": "positive",
            "data": {"action": "invite_user", "tab": "add-user"},
        }
    ]
    return body, actions


def _interactive_policy_blocks(payload: dict) -> tuple[list[dict], list[dict]]:
    signed = payload.get("signed_count") or 0
    unsigned = payload.get("unsigned_count") or 0
    meta_bits = [
        payload.get("policy_file_name"),
        payload.get("policy_version"),
        f"effective {payload['policy_effective_date']}" if payload.get("policy_effective_date") else "",
        f"uploaded by {payload['policy_uploaded_by']}" if payload.get("policy_uploaded_by") else "",
    ]
    meta_bits = [b for b in meta_bits if b]
    body = [
        {
            "type": "TextBlock",
            "text": " · ".join(meta_bits) if meta_bits else "No policy uploaded yet.",
            "size": "Small",
            "isSubtle": True,
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
                        {"type": "TextBlock", "text": "SIGNED", "size": "Small", "isSubtle": True, "spacing": "None"},
                        {"type": "TextBlock", "text": str(signed), "size": "ExtraLarge", "weight": "Bolder", "color": "good", "spacing": "None"},
                    ],
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {"type": "TextBlock", "text": "UNSIGNED", "size": "Small", "isSubtle": True, "spacing": "None"},
                        {
                            "type": "TextBlock",
                            "text": str(unsigned),
                            "size": "ExtraLarge",
                            "weight": "Bolder",
                            "color": "attention" if unsigned else "good",
                            "spacing": "None",
                        },
                    ],
                },
            ],
        },
    ]
    actions = []
    policy_url = payload.get("policy_url") or ""
    if policy_url:
        if policy_url.startswith("/"):
            # Our own PDF endpoint (teams_admin_policy_file) — open it in a
            # Teams Task Module (in-app modal) instead of a browser tab.
            actions.append(
                {
                    "type": "Action.Submit",
                    "title": "View Current Policy",
                    "style": "positive",
                    "data": {
                        "msteams": {"type": "task/fetch"},
                        "action": "view_policy",
                        "email": payload.get("email", ""),
                    },
                }
            )
        else:
            # Default fallback policy lives on an external site we don't
            # control (not framing-safe) — leave this one as a real link out.
            actions.append({"type": "Action.OpenUrl", "title": "View Current Policy", "url": policy_url, "style": "positive"})
    # Adaptive Cards have no file-upload input at all — publishing a new PDF
    # version needs a real webpage, so this opens the Website Tab's Policy
    # page as a Task Module (in-app modal) instead of a browser tab.
    actions.append(
        {
            "type": "Action.Submit",
            "title": "Upload New Policy (PDF)",
            "data": {
                "msteams": {"type": "task/fetch"},
                "action": "upload_policy",
                "email": payload.get("email", ""),
            },
        }
    )
    return body, actions


_CARD_STATUS_LABELS = {
    "not_sent": "Not sent",
    "requested": "Requested",
    "sent": "Sent",
    "scheduled": "Scheduled",
    "failed": "Failed",
}


def _interactive_cards_blocks(payload: dict) -> tuple[list[dict], list[dict]]:
    catalog = payload.get("catalog") or CARD_CATALOG
    quota_used = payload.get("quota_used", 0)
    quota_max = payload.get("quota_max", 10)
    body: list[dict] = [
        {
            "type": "Container",
            "style": "warning",
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock", "text": "MONTHLY CARD QUOTA", "size": "Small", "weight": "Bolder", "spacing": "None"},
                {
                    "type": "TextBlock",
                    "text": f"{quota_used} of {quota_max} cards requested this month",
                    "weight": "Bolder",
                    "size": "Medium",
                    "spacing": "Small",
                },
            ],
        }
    ]
    choices = []
    for c in catalog:
        status_label = _CARD_STATUS_LABELS.get(c.get("status"), c.get("status") or "")
        rating_num = (c.get("rating") or "").split(" ")[0]
        meta = " · ".join(x for x in [c.get("category"), c.get("price"), (f"★{rating_num}" if rating_num else "")] if x)
        body.append(
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Small",
                "items": [
                    {"type": "TextBlock", "text": f"**{c.get('icon', '')} {c.get('title')}** · {status_label}", "wrap": True, "spacing": "None"},
                    {"type": "TextBlock", "text": meta, "size": "Small", "isSubtle": True, "wrap": True, "spacing": "None"},
                    {
                        "type": "ActionSet",
                        "spacing": "Small",
                        "actions": [
                            {
                                "type": "Action.Execute",
                                "title": "Request",
                                "verb": "aidl.card_request",
                                "data": {"action": "request_card", "tab": "cards", "card_id": c["id"]},
                            },
                            {
                                "type": "Action.Execute",
                                "title": "Send Now",
                                "verb": "aidl.card_send_now",
                                "style": "positive",
                                "data": {"action": "send_card_now", "tab": "cards", "card_id": c["id"]},
                            },
                        ],
                    },
                ],
            }
        )
        choices.append({"title": f"{c.get('icon', '')} {c.get('title')}", "value": c["id"]})

    body += [
        {"type": "TextBlock", "text": "SCHEDULE A CARD", "size": "Small", "weight": "Bolder", "isSubtle": True, "spacing": "Medium"},
        {"type": "Input.ChoiceSet", "id": "scheduleCardId", "style": "compact", "placeholder": "Pick a card", "choices": choices},
        {"type": "Input.Date", "id": "scheduleDate", "label": "Date"},
        {"type": "Input.Time", "id": "scheduleTime", "label": "Time"},
        {"type": "TextBlock", "text": "REQUEST A NEW CARD", "size": "Small", "weight": "Bolder", "isSubtle": True, "spacing": "Medium"},
        {"type": "Input.Text", "id": "newCardTitle", "label": "Title", "placeholder": "New card title"},
        {"type": "Input.Text", "id": "newCardDesc", "placeholder": "What should it cover? (optional)", "isMultiline": True},
    ]
    actions = [
        {
            "type": "Action.Execute",
            "title": "Schedule",
            "verb": "aidl.card_schedule",
            "data": {"action": "schedule_card", "tab": "cards"},
        },
        {
            "type": "Action.Execute",
            "title": "Request New Card",
            "verb": "aidl.card_request_new",
            "data": {"action": "request_new_card", "tab": "cards"},
        },
    ]
    return body, actions


_DATA_ALLOWED_LABELS = {
    "public_only": "Public only",
    "internal": "Internal",
    "internal_confidential": "Internal + Confidential",
    "none": "None",
}


def _interactive_app_list_blocks(tab: str, payload: dict) -> tuple[list[dict], list[dict]]:
    items = payload.get("items") or []
    rows = []
    for it in items[:10]:
        app_status = (it.get("status") or "pending").lower()
        color = {"approved": "good", "pending": "warning", "rejected": "attention"}.get(app_status)
        label = "Prohibited" if app_status == "rejected" else app_status.title()
        meta = " · ".join(x for x in [it.get("category"), _DATA_ALLOWED_LABELS.get(it.get("data_allowed"), "")] if x) or it.get("description") or ""
        status_block = {"type": "TextBlock", "text": label.upper(), "size": "Small", "weight": "Bolder", "wrap": False}
        if color:
            status_block["color"] = color
        rows.append(
            {
                "type": "Container",
                "style": "emphasis",
                "spacing": "Small",
                "items": [
                    {
                        "type": "ColumnSet",
                        "columns": [
                            {
                                "type": "Column",
                                "width": "stretch",
                                "items": [
                                    {"type": "TextBlock", "text": f"**{it.get('name') or 'App'}**", "wrap": True, "spacing": "None"},
                                    {"type": "TextBlock", "text": meta, "size": "Small", "isSubtle": True, "wrap": True, "spacing": "None"},
                                ],
                            },
                            {"type": "Column", "width": "auto", "verticalContentAlignment": "Center", "items": [status_block]},
                        ],
                    }
                ],
            }
        )
    body = rows or [{"type": "TextBlock", "text": "No applications registered yet.", "isSubtle": True, "wrap": True}]
    body += [
        {"type": "TextBlock", "text": "ADD APPLICATION", "size": "Small", "weight": "Bolder", "isSubtle": True, "spacing": "Medium"},
        {"type": "Input.Text", "id": "appName", "label": "Name", "placeholder": "App name"},
        {"type": "Input.Text", "id": "appCategory", "placeholder": "Category (optional)"},
        {
            "type": "Input.ChoiceSet",
            "id": "appDataAllowed",
            "style": "compact",
            "placeholder": "Data allowed…",
            "choices": [{"title": v, "value": k} for k, v in _DATA_ALLOWED_LABELS.items()],
        },
        {
            "type": "Input.ChoiceSet",
            "id": "appStatus",
            "style": "compact",
            "value": "pending",
            "choices": [
                {"title": "Pending", "value": "pending"},
                {"title": "Approved", "value": "approved"},
                {"title": "Prohibited", "value": "rejected"},
            ],
        },
    ]
    actions = [
        {
            "type": "Action.Execute",
            "title": "Add Application",
            "verb": "aidl.app_add",
            "style": "positive",
            "data": {"action": "add_app", "tab": tab, "app_type": "ai" if tab == "ai-apps" else "it"},
        }
    ]
    return body, actions


def _section_card(
    tab: str,
    *,
    full_name: str,
    org_name: str,
    email: str,
    user,
    interactive: bool = False,
) -> dict:
    payload = build_admin_tab_payload(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    org_id = payload.get("organization_id") or ""
    org = payload.get("org_name") or org_name or "AIDL"

    if tab == "add-admin":
        content_blocks, actions = _interactive_add_admin_blocks(payload, user=user)
    elif tab == "add-user":
        content_blocks, actions = _interactive_add_user_blocks(payload, user=user)
    elif tab == "policy":
        content_blocks, actions = _interactive_policy_blocks(payload)
    elif tab == "cards":
        content_blocks, actions = _interactive_cards_blocks(payload)
    elif tab in ("ai-apps", "it-apps"):
        content_blocks, actions = _interactive_app_list_blocks(tab, payload)
    else:
        content_blocks, actions = _generic_item_blocks(payload.get("items") or []), []

    body = [
        _header(org),
        {
            "type": "TextBlock",
            "text": "Admin Center",
            "size": "Small",
            "weight": "Bolder",
            "spacing": "Medium",
        },
        _nav_container(
            tab,
            email=email,
            org_id=org_id,
            interactive=interactive,
        ),
        {
            "type": "TextBlock",
            "text": payload.get("heading") or payload.get("title") or tab,
            "size": "Large",
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": payload.get("body") or "",
            "wrap": True,
        },
        *content_blocks,
    ]
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "msteams": {"width": "Full"},
    }
    if actions:
        card["actions"] = actions
    return card
