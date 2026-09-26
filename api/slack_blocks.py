"""Block Kit versions of the Slack Admin cards (AIDL Slack guide, section 8).

Each tab is one message: the AIDL app line, the card content and a row of
tab buttons (Home, Add Admin, Add User, Cards, AI Apps, IT Apps — no Policy
tab). Tab clicks are handled in slack_interactions.py; the full forms
(invite, issue license, request/send cards, add apps) open the responsive
card page with a short-lived link only the clicking admin can see.
"""

from __future__ import annotations

from .slack_cards import ADMIN_TABS

_DOT = {"g": "🟢", "a": "🟡", "r": "🔴"}
_OPEN_LABEL = {
    "home": "⬇ Export Coverage CSV",
    "add-admin": "Send Admin Invite →",
    "add-user": "Issue License →",
    "cards": "View & send cards →",
    "ai-apps": "＋ Add AI Application →",
    "it-apps": "＋ Add IT Application →",
}
_NEXT = {
    "home": "→ Next: bring in another admin to help run this",
    "add-admin": "→ Next: add your team members",
    "add-user": "→ Next: send your team the reference cards they'll need",
    "cards": "→ Next: set which AI tools your team is allowed to use",
    "ai-apps": "→ Next: do the same for your IT systems",
    "it-apps": "✓ That's the full admin setup flow",
}


def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _section(text: str, **extra) -> dict:
    return {"type": "section", "text": _md(text), **extra}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [_md(text)]}


def _button(text: str, action_id: str, *, value: str = "", url: str = "", style: str = "") -> dict:
    btn = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True}, "action_id": action_id}
    if value:
        btn["value"] = value
    if url:
        btn["url"] = url
    if style:
        btn["style"] = style
    return btn


def tab_buttons(d: dict, active: str) -> dict:
    return {
        "type": "actions",
        "block_id": "aidl_tabs",
        "elements": [
            _button(f"{icon} {label}", f"aidl_tab_{tab}", value=tab, style="primary" if tab == active else "")
            for tab, label, icon in ADMIN_TABS
            if tab in d["tabs"]
        ],
    }


def _open_button(tab: str, open_url: str) -> dict:
    """In a private (ephemeral) card the button links straight to the form;
    in the shared channel message it asks for a private link first."""
    if open_url:
        return _button(_OPEN_LABEL[tab], f"aidl_link_{tab}", url=open_url, style="primary")
    return _button(_OPEN_LABEL[tab], f"aidl_open_{tab}", value=tab)


def _home(d: dict) -> list:
    aup = f"*{d['aup_display']}*" + (" ⚠️" if d["aup_warn"] else "")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"👋 Welcome to your Admin Center, {d['admin_name']}", "emoji": True}},
        _section("You provision the seats, set the house rules, and keep everything running smoothly. Your team does the driving."),
        {
            "type": "section",
            "fields": [
                _md(f"*SEATS PURCHASED*\n*{d['seats_purchased']}* · renews {d['seats_renew']}"),
                _md(f"*LICENSES ISSUED*\n*{d['licences_issued']}* · {d['licence_pct']}% of enrolled"),
                _md(f"*AUP UNSIGNED*\n{aup} · blocks ethics gate"),
            ],
        },
        _context("*GOVERNANCE SNAPSHOT*"),
        {
            "type": "section",
            "fields": [
                _md(f"*TOTAL ADMINS*\n*{d['admin_count']}* of {d['admin_seat_limit']} seats"),
                _md(f"*TOTAL APPROVED*\n*{d['approved_apps']}* of {d['total_apps']} apps"),
                _md(f"*AI APPLICATIONS*\n*{len(d['ai_apps'])}* in registry"),
                _md(f"*IT APPLICATIONS*\n*{len(d['it_apps'])}* in registry"),
            ],
        },
    ]


def _add_admin(d: dict) -> list:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "🧑‍💼 Add another admin", "emoji": True}},
        _section("Give a colleague access to your Admin Center — choose exactly what they can see and do.\n"
                 "*Permissions:* Approve Apps · Access Cards · Create Card"),
        _context(f"{d['admin_count']} of {d['admin_seat_limit']} admin seats used"),
    ]


def _add_user(d: dict) -> list:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "👤 Add a team member", "emoji": True}},
        _section("Issue a license to a team member — they get the reference cards and can request more for their channel. No Admin Center access."),
        _context(f"{d['licences_issued']} of {d['seats_purchased']} licenses issued"),
    ]


def _cards(d: dict) -> list:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📬 Send Cards to your team", "emoji": True}},
        _section("Pick the reference cards your team needs at the wheel — view one, request it, then send it into the channel where they already work."),
        _section(f"*MONTHLY CARD QUOTA*\n`{d['quota_used']} of {d['quota_max']}` cards requested this month\n"
                 f"📅 All cards available {d['available_from']} – {d['available_to']}"),
    ]
    if d["can_manage_cards"]:
        blocks.append({"type": "divider"})
        for c in d["cards"]:
            state = {"sent": "  ✓ Sent", "scheduled": "  🕒 Scheduled", "requested": "  ✓ Requested"}.get(c["state"], "")
            badge = "  ⭐ _Recommended_" if c.get("recommended") else ""
            price = "`Not available yet`" if c["state"] == "unavailable" else f"`{c['price']} · one-time`"
            blocks.append(_section(
                f"{c['icon']} *{c['title']}*{badge}{state}\n_{c['kicker'].upper()}_\n{c['desc']}\n"
                f"{price}  ★ *{c['rating']}* ({c['votes']})"
            ))
    if d["can_create_card"]:
        blocks.append(_context("*Need something that's not listed?* Request a brand-new reference card — we'll scope it and follow up with pricing."))
    return blocks


def _apps(d: dict, kind: str) -> list:
    rows = d["ai_apps"] if kind == "ai" else d["it_apps"]
    title = "🤖 AI Applications" if kind == "ai" else "💻 IT Applications"
    intro = "The AI tools your team is allowed to use." if kind == "ai" else "The IT systems your team is allowed to use alongside AI."
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        _section(intro),
        {"type": "divider"},
    ]
    if not rows:
        blocks.append(_context("No applications yet."))
    for a in rows:
        pill = "✅ Approved" if a["status"] == "Approved" else "⛔ Prohibited"
        owner = f" · {a['owner']}" if a["owner"] else ""
        blocks.append(_section(f"*{a['name']}*   {pill}\n{a['category']} · {_DOT[a['data_dot']]} {a['data_allowed']}{owner}"))
    return blocks


_BUILDERS = {
    "home": _home,
    "add-admin": _add_admin,
    "add-user": _add_user,
    "cards": _cards,
    "ai-apps": lambda d: _apps(d, "ai"),
    "it-apps": lambda d: _apps(d, "it"),
}


def admin_card_blocks(d: dict, tab: str = "home", *, open_url: str = "") -> list:
    """Blocks for one Admin card. `open_url` (a private, signed link) is only
    passed for ephemeral messages that just the clicking admin can see."""
    if tab not in d["tabs"]:
        tab = "home"
    blocks = [_context(f"*AIDL* · APP  for {d['org_name']}")]
    blocks += _BUILDERS[tab](d)
    if tab != "home" or d["tabs"] == [t for t, _l, _i in ADMIN_TABS]:
        blocks.append({"type": "actions", "block_id": "aidl_open", "elements": [_open_button(tab, open_url)]})
    if tab == "home":
        blocks.append(_context(f"{d['rollout_done']} of {d['rollout_total']} rollout steps done · {d['org_name']}"))
    blocks.append(_context(_NEXT[tab]))
    blocks.append(tab_buttons(d, tab))
    return blocks


def admin_card_text(d: dict) -> str:
    return f"AIDL Admin Center for {d['org_name']}"


def publish_admin_center(org, user) -> bool:
    """Post (or refresh) the Admin Center Home card in the org's channel —
    after login, and again whenever the policy answers change."""
    from . import slack_client
    from .slack_cards import build_admin_cards_context

    data = build_admin_cards_context(user)
    return slack_client.post_admin_center(org, admin_card_blocks(data, "home"), admin_card_text(data))
