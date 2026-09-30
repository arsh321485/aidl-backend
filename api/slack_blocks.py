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
    "add-admin": "Send Admin Invite",
    "add-user": "Issue License",
    "cards": "View & send cards",
    "ai-apps": "＋ Add AI Application",
    "it-apps": "＋ Add IT Application",
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
    """Every card button opens a Slack modal (slack_modals.py) — Export
    Coverage CSV too: the report shows in the modal and the CSV file is sent
    to the admin by DM, so nothing opens in the browser."""
    return _button(_OPEN_LABEL[tab], f"aidl_open_{tab}", value=tab, style="" if tab == "home" else "primary")


def _home(d: dict) -> list:
    """Home — Depot Overview (guide 8.1), in the layout the team signed off
    (aidl_admin_home.json). All values come from build_admin_cards_context."""
    name = d["admin_name"]
    enrolled = d["enrolled"]
    pct = d["licence_pct"]
    filled = round(pct / 10)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🚦 AIDL Admin Center", "emoji": True}},
        _context(f"*{d['org_name']}*  ·  AI Driving License  ·  Signed in as *{name}*"),
        _section(f"👋 *Welcome back, {name}.*\nYou provision the seats, set the house rules, and keep "
                 "everything running smoothly. _Your team does the driving._"),
        {"type": "divider"},
        _section("📊  *LICENSES & SEATS*"),
        {"type": "section", "fields": [
            _md(f"*Seats purchased*\n*{d['seats_purchased']}* seats  ·  _renews {d['seats_renew']}_"),
            _md(f"*Licenses issued*\n*{d['licences_issued']}* of {enrolled} enrolled  ·  {pct}%\n"
                + "▰" * filled + "▱" * (10 - filled)),
        ]},
        _aup_block(d),
        {"type": "divider"},
        _section("🛡️  *GOVERNANCE SNAPSHOT*"),
        {"type": "section", "fields": [
            _md(f"*👥 Admins*\n*{d['admin_count']}* of {d['admin_seat_limit']} seats"
                + ("  ·  _at capacity_" if d["admin_count"] >= d["admin_seat_limit"] else "")),
            _md(f"*✅ Apps approved*\n*{d['approved_apps']}* of {d['total_apps']}"
                + (f"  ·  {d['total_apps'] - d['approved_apps']} prohibited"
                   if d["total_apps"] > d["approved_apps"] else "")),
            _md(f"*🤖 AI applications*\n*{len(d['ai_apps'])}* in registry"),
            _md(f"*💻 IT applications*\n*{len(d['it_apps'])}* in registry"),
        ]},
        {"type": "divider"},
        _rollout_block(d),
    ]
    return blocks


def _aup_block(d: dict) -> dict:
    """AUP status — a red alert with Send reminders when people haven't signed."""
    if d["policy"].get("aup_status") == "not_available":
        return _context("📄 No written AI policy yet — AUP signatures start once it's published.")
    if not d["aup_warn"]:
        return _section("✅ *Everyone has signed the AUP*")
    n = d["aup_unsigned"]
    return {
        "type": "section",
        "text": _md(f"🚨 *{n} user{'s' if n != 1 else ''} haven't signed the AUP*\n"
                    "They're blocked at the ethics gate until they do."),
        "accessory": _button("Send reminders", "aidl_aup_remind", value="remind", style="danger"),
    }


def _next_setup_tab(d: dict) -> str:
    """The tab 'Finish setup →' opens — the first setup step still open."""
    order = ["add-admin", "add-user", "cards", "ai-apps"]
    if d["admin_count"] >= d["admin_seat_limit"]:
        order.remove("add-admin")
    return next((t for t in order if t in d["tabs"]), "")


def _rollout_block(d: dict) -> dict:
    done, total = d["rollout_done"], d["rollout_total"]
    pct = round(done / total * 100) if total else 0
    block = {"type": "section", "text": _md(
        f"🚀 *Rollout progress*  ·  {done} of {total} steps done\n"
        + "🟩" * done + "⬜" * max(total - done, 0) + f"  *{pct}%*")}
    next_tab = _next_setup_tab(d)
    if done < total and next_tab:
        # Reuses the existing tab handler (aidl_tab_*), no new navigation.
        block["accessory"] = _button("Finish setup →", f"aidl_tab_{next_tab}", value=next_tab, style="primary")
    return block


def has_images(blocks: list) -> bool:
    return any(b.get("type") == "image" or any(e.get("type") == "image" for e in b.get("elements") or [])
               for b in blocks)


def text_fallback(blocks: list) -> list:
    """Same card without images — the logo is dropped from the brand line if
    Slack can't load it."""
    out = []
    for block in blocks:
        if block.get("type") == "image":
            continue
        elif block.get("type") == "context":
            elements = [e for e in block["elements"] if e.get("type") != "image"]
            if elements:
                out.append({**block, "elements": elements})
        else:
            out.append(block)
    return out


def _app_line(org_name: str) -> dict:
    """Brand line at the top of every AIDL card, so it's recognisable at a
    glance: 'AIDL · AI Driving License' + the organization."""
    # The AIDL logo is the Slack app icon (shown next to "aidl" on every
    # message), so it isn't repeated here.
    return {"type": "context", "elements": [
        _md(f"*🚦 AIDL · AI Driving License*   |   Admin Center for *{org_name}*"),
    ]}


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
    # Navigation first, so the tabs are the first thing an admin sees.
    if tab == "home":
        return [tab_buttons(d, tab)] + _home(d) + [
            {"type": "actions", "block_id": "aidl_open", "elements": [_open_button("home", open_url)]},
            _context("AIDL · AI Driving License"),
        ]
    blocks = [tab_buttons(d, tab), _app_line(d["org_name"])]
    blocks += _BUILDERS[tab](d)
    if tab != "home" or d["tabs"] == [t for t, _l, _i in ADMIN_TABS]:
        blocks.append({"type": "actions", "block_id": "aidl_open", "elements": [_open_button(tab, open_url)]})
    if tab == "home":
        blocks.append(_context(_rollout_line(d)))
    blocks.append(_context(_NEXT[tab]))
    return blocks


def admin_card_text(d: dict) -> str:
    return f"AIDL Admin Center for {d['org_name']}"


def publish_admin_center(org, user) -> bool:
    """Post (or refresh) the Admin Center Home card in the org's channel —
    after login, and again whenever the policy answers change."""
    from . import slack_client
    from .slack_cards import build_admin_cards_context

    from .models import AIDLUser

    # The shared channel card greets the admin who signed the organization up.
    primary = (
        AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True)
        .order_by("created_at")
        .first()
    ) or user
    data = build_admin_cards_context(primary)
    return slack_client.post_admin_center(org, admin_card_blocks(data, "home"), admin_card_text(data))


# ---------- User cards (guide 10.1, 10.2) — sent by DM ----------

def user_welcome_blocks(member, org) -> list:
    """Welcome message only — no buttons, nothing asked of the user."""
    first = (member.full_name or "there").split()[0]
    return [
        _context(f"*AIDL* · APP  for {org.name}"),
        {"type": "header", "text": {"type": "plain_text", "text": f"Welcome to AIDL, {first}! 👋", "emoji": True}},
        _section(f"You've been added to *{org.name}'s* AI Driving License programme."),
    ]


def user_license_blocks(member, org) -> list:
    from django.conf import settings

    from .org_policy import get_answers, policy_effects
    from .slack_cards import CURRICULUM_VERSION, DEFAULT_POLICY_VERSION

    issued = member.licence_issued_at.strftime("%m/%d/%Y") if member.licence_issued_at else "—"
    expires = member.licence_expires_at.strftime("%m/%d/%Y") if member.licence_expires_at else "—"
    frontend = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    verify = f"{frontend}/home#verify?lic={member.licence_number}"
    note = policy_effects(get_answers(org))["training_note"]
    card = {
        "type": "section",
        "text": _md(f"*AI DRIVING LICENSE* · issued for {org.name}\n*{(member.full_name or '').upper()}*"),
        "fields": [
            _md("*CLASS*\nLearner's Permit (L)"),
            _md(f"*STATUS*\n{'ACTIVE' if member.licence_issued else 'PENDING'}"),
            _md(f"*ISSUED*\n{issued}"),
            _md(f"*EXPIRES*\n{expires}"),
        ],
    }
    if member.avatar_url:
        card["accessory"] = {"type": "image", "image_url": member.avatar_url, "alt_text": "Driver photo"}
    from urllib.parse import quote

    share_text = quote(f"I just earned my AI Driving License from {org.name}!")
    link = quote(verify, safe="")
    initials = "".join(w[0] for w in org.name.split()[:2]).upper() or "AI"
    blocks = [
        _context(f"*AIDL* · APP  for {org.name}"),
        {"type": "header", "text": {"type": "plain_text", "text": "🪪 Your Learner's Permit is ready", "emoji": True}},
        # Guide 10.2: the L / F switch shows the real level only.
        _context("🟨 *L · Learner*  ← current level      ⬜ F · Full"),
        card,
        _context(f"*{initials}* · Issued for {org.name} · `{member.licence_number}` · verify at aidl.org/verify"),
        _section("This is your AI Driving License. You're currently at *Level L — Learner.* "
                 "As you complete lessons and pass checks, you'll move up to higher levels."),
        {"type": "actions", "elements": [
            _button("⬇ Download License", "aidl_link_download",
                    url=f"{_backend_base()}/api/teams/cards/learners-permit/download/?email={quote(member.email)}"),
        ]},
        {"type": "actions", "elements": [
            _button("𝕏 Share", "aidl_link_x", url=f"https://twitter.com/intent/tweet?text={share_text}&url={link}"),
            _button("in LinkedIn", "aidl_link_linkedin", url=f"https://www.linkedin.com/sharing/share-offsite/?url={link}"),
            _button("f Facebook", "aidl_link_facebook", url=f"https://www.facebook.com/sharer/sharer.php?u={link}"),
            _button("WhatsApp", "aidl_link_whatsapp", url=f"https://wa.me/?text={share_text}%20{link}"),
        ]},
        _context(f"Issued under Curriculum {CURRICULUM_VERSION} · Org Policy {DEFAULT_POLICY_VERSION} — recorded at issuance"),
    ]
    if note:
        blocks.append(_context(f"🔁 {note}"))
    return blocks


# Guide 10.3 — road-sign emoji per Highway Code rule shape.
_SIGN = {"stop": "🛑 STOP", "check": "🔶 CHECK", "yield": "⚠️ YOU", "ask": "🔵 ASK", "oneway": "⬛ ONE WAY"}


def highway_code_blocks(org) -> list:
    """Section A · Learner Rules — sent right after the License card."""
    from .org_policy import get_answers, policy_effects
    from .slack_cards import _highway_code

    rules = _highway_code(policy_effects(get_answers(org)))
    blocks = [
        _context(f"*AIDL* · APP  for {org.name}"),
        {"type": "header", "text": {"type": "plain_text", "text": "📖 The Highway Code", "emoji": True}},
        _section("The everyday rules for using AI safely and confidently. Learn them, follow them, drive happy."),
        _context("*SECTION A · LEARNER RULES*"),
    ]
    for rule in rules:
        blocks.append(_section(f"*{_SIGN[rule['shape']]}* — *{rule['title']}*\n{rule['text']}"))
    blocks.append({"type": "actions", "elements": [
        _button("Open full Highway Code", "aidl_hc_full", value="full", style="primary")]})
    return blocks


def highway_code_full_view(org) -> dict:
    from .org_policy import get_answers, policy_effects
    from .slack_cards import _highway_code

    rules = _highway_code(policy_effects(get_answers(org)))
    blocks = [_context(f"*{org.name.upper()} · LEARNER RULES* · Section A · 5 rules, expanded")]
    for rule in rules:
        blocks.append(_section(f"*{_SIGN[rule['shape']]} — {rule['title']}*\n{rule['text']} {rule['full']}"))
    blocks += [{"type": "divider"}, _context("Reference only — no sign-off needed")]
    return {
        "type": "modal",
        "callback_id": "aidl_highway_full",
        "title": {"type": "plain_text", "text": "The Highway Code"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


def rating_blocks(likes: int, dislikes: int) -> list:
    """Guide 10.4 — 'Was this card useful?' 👍 / 👎 under Traffic Light Check."""
    return [
        _context("*Was this card useful?*"),
        {"type": "actions", "block_id": "aidl_rate", "elements": [
            _button(f"👍 {likes}", "aidl_rate_like", value="like"),
            _button(f"👎 {dislikes}", "aidl_rate_dislike", value="dislike"),
        ]},
    ]


def _backend_base() -> str:
    from django.conf import settings

    redirect = getattr(settings, "SLACK_REDIRECT_URI", "") or ""
    return redirect.split("/api/")[0] if "/api/" in redirect else "https://aidl-backend.onrender.com"


# ---------- User dashboard (guide 10): Home · License · Highway Code · Traffic Light Check ----------

USER_TABS = (
    ("home", "🏠 Home"),
    ("license", "🪪 License"),
    ("highway-code", "📖 Highway Code"),
    ("traffic-light", "🚦 Traffic Light Check"),
)


def _sent_traffic_light(org):
    """The last Traffic Light Check the admin sent (guide 10.4), or None."""
    from .models import CardRequest

    return (CardRequest.objects.filter(organization_id=str(org.pk), card_id="c1", status=CardRequest.Status.SENT)
            .order_by("-sent_at").first())


def user_traffic_light_blocks(org) -> list:
    from .models import TrafficLightRating
    from .org_policy import get_answers, policy_effects
    from .slack_cards import TRAFFIC_LIGHTS

    blocks = [
        _context(f"*AIDL* · APP  for {org.name}"),
        {"type": "header", "text": {"type": "plain_text", "text": "🚦 Traffic Light Check", "emoji": True}},
    ]
    sent = _sent_traffic_light(org)
    if sent is None:
        blocks.append(_section("Your admin hasn't sent the Traffic Light Check yet — it will show here once they do."))
        return blocks
    chosen = [l for l in (sent.lights or "green,amber,red").split(",") if l]
    fx = policy_effects(get_answers(org))
    emoji = {"green": "🟢", "amber": "🟡", "red": "🔴"}
    blocks.append(_section("Before you paste anything into an AI, check the lights."))
    for light in TRAFFIC_LIGHTS:
        if light["id"] not in chosen:
            continue
        items = list(light["items"])
        if light["id"] == "red" and fx["red_includes_confidential"]:
            items.append("All confidential and customer data")
        blocks.append(_section(f"{emoji[light['id']]} *{light['label']}* — _{light['tagline']}_\n"
                               + "\n".join(f"• {i}" for i in items) + f"\n*→ {light['action']}*"))
    rating = TrafficLightRating.objects.first()
    return blocks + rating_blocks(rating.likes if rating else 128, rating.dislikes if rating else 6)


def user_tab_buttons(active: str) -> dict:
    return {
        "type": "actions",
        "block_id": "aidl_user_tabs",
        "elements": [
            _button(label, f"aidl_utab_{tab}", value=tab, style="primary" if tab == active else "")
            for tab, label in USER_TABS
        ],
    }


def user_dashboard_blocks(member, org, tab: str = "license") -> list:
    """One message with the prototype's tab bar; clicking a tab swaps the
    card shown in the message (slack_interactions.py)."""
    content = {
        "home": lambda: user_welcome_blocks(member, org),
        "license": lambda: user_license_blocks(member, org),
        "highway-code": lambda: highway_code_blocks(org),
        "traffic-light": lambda: user_traffic_light_blocks(org),
    }.get(tab) or (lambda: user_license_blocks(member, org))
    return content() + [{"type": "divider"}, user_tab_buttons(tab)]
