"""Block Kit versions of the Slack Admin cards (AIDL Slack guide, section 8).

Each tab is one message: the AIDL app line, the card content and a row of
tab buttons (Home, Add Admin, Cards, AI Apps, IT Apps — no Policy
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
    "cards": "View & send cards",
    "ai-apps": "＋ Add AI Application",
    "it-apps": "＋ Add IT Application",
}
_NEXT = {
    "home": "→ Next: bring in another admin to help run this",
    "add-admin": "→ Next: add your team to the AIDL channel — AIDL onboards them automatically",
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


# ---------- Admin Center: step-by-step setup (admin_setup.py) ----------

_DEMO_SETUP = {"aup_done": False, "delegate_name": "", "admins_done": False, "team_done": False,
               "cards_done": False, "members": 0, "done_count": 0,
               "sends": {i: {"sent": False, "accepted": 0} for i in ("aup", "traffic_light", "highway_code")}}
_SEND_LABEL = {"aup": "📄 AI Acceptable Use Policy", "traffic_light": "🚦 Traffic Light Check",
               "highway_code": "📖 Highway Code"}


def _actions(block_id: str, *buttons) -> dict:
    return {"type": "actions", "block_id": block_id, "elements": [b for b in buttons if b]}


def _locked(n: int, title: str, after: str) -> dict:
    return _context(f"⬜  *{n} · {title}*  —  _after {after}_")


def _step_aup(d: dict, st: dict) -> list:
    if not st["aup_done"]:
        blocks = [_section("*1 · Create your AI policy (AUP)*\nAnswer 8 quick questions about how your organization "
                           "uses AI — AIDL writes your team's AI Acceptable Use Policy from them.")]
        if st.get("delegate_name"):
            blocks.append(_context(f"⏳ Assigned to *{st['delegate_name']}* — waiting for them to create it."))
        if d["can_policy"]:
            blocks.append(_actions("aidl_step_aup",
                                   _button("📝 Create AUP", "aidl_policy_open", value="policy", style="primary"),
                                   _button("🙋 Ask someone else", "aidl_aup_delegate", value="delegate")
                                   if d["can_admins"] else None))
        return blocks
    apps = f"{d['approved_apps']} of {d['total_apps']} apps approved"
    return [
        _section(f"✅  *1 · AI policy (AUP)*  ·  `{d['team'].get('aup_version', '')}`  ·  {apps}"),
        _actions("aidl_step_aup",
                 _button("📄 View AUP", "aidl_aup_view", value="view"),
                 _button("✏️ Edit answers", "aidl_policy_open", value="policy") if d["can_policy"] else None,
                 _button("🤖 AI apps", "aidl_tab_ai-apps", value="ai-apps") if d["can_apps"] else None,
                 _button("💻 IT apps", "aidl_tab_it-apps", value="it-apps") if d["can_apps"] else None),
    ]


def _step_admins(d: dict, st: dict) -> list:
    seats = f"{d['admin_count']} of {d['admin_seat_limit']} admin seats used"
    if not st["aup_done"]:
        return [_locked(2, "Add admins (optional)", "step 1")]
    if not st["admins_done"]:
        blocks = [_section("*2 · Add admins (optional)*\nInvite HR, IT or Compliance colleagues to help run AIDL — "
                           f"you choose what each one can do.  ·  _{seats}_")]
        if d["can_admins"]:
            blocks.append(_actions("aidl_step_admins",
                                   _button("🧑‍💼 Add admin", "aidl_open_add-admin", value="add-admin", style="primary"),
                                   _button("Skip →", "aidl_setup_skip", value="admins")))
        return blocks
    return [
        _section(f"✅  *2 · Admins*  ·  {seats}"),
        *([_actions("aidl_step_admins", _button("🧑‍💼 Add or remove admins", "aidl_open_add-admin", value="add-admin"))]
          if d["can_admins"] else []),
    ]


def _step_team(d: dict, st: dict) -> list:
    if not (st["aup_done"] and st["admins_done"]):
        return [_locked(3, "Add your team", "step 2")]
    p = d.get("plan") or {}
    seats = f"{p.get('used', st['members'])} of {p.get('limit', '—')} users on your {p.get('label', '')} plan".strip()
    head = ("*3 · Add your team*\nAdd the people who should earn an AI licence. They join the AIDL channel and "
            "get their cards automatically." if not st["team_done"]
            else f"✅  *3 · Team*  ·  {st['members']} member{'s' if st['members'] != 1 else ''}")
    blocks = [_section(f"{head}  ·  _{seats}_")]
    blocks += _plan_notice(d)
    if d["can_team"]:
        blocks.append(_actions("aidl_step_team",
                               _button("👥 Add people", "aidl_add_people", value="team",
                                       style="" if st["team_done"] else "primary"),
                               _button("View team progress", "aidl_team_progress", value="team") if st["team_done"] else None))
    return blocks


def _step_send(d: dict, st: dict) -> list:
    if not (st["aup_done"] and st["team_done"]):
        return [_locked(4, "Send the learning cards", "step 3")]
    head = ("*4 · Send the learning cards*\nSend each card to your team once — people who join later get it "
            "automatically. Accepting the Highway Code and Traffic Light Check earns the licence."
            if not st["cards_done"] else "✅  *4 · Learning cards sent*")
    blocks = [_section(head)]
    members = st["members"]
    for item in ("aup", "traffic_light", "highway_code"):
        s = st["sends"][item]
        line = f"{_SEND_LABEL[item]}\n" + (f"✓ Sent  ·  *{s['accepted']}* of {members} accepted" if s["sent"]
                                             else "_Not sent yet_")
        block = _section(line)
        if d["can_send"]:
            if not s["sent"]:
                block["accessory"] = _button("📤 Send", "aidl_send_item", value=item, style="primary")
            elif s["accepted"] < members:
                block["accessory"] = _button("🔔 Remind", "aidl_remind_item", value=item)
        blocks.append(block)
    return blocks


def _step_licences(d: dict, st: dict) -> list:
    if not st["cards_done"]:
        return [_locked(5, "Licences & awareness cards", "step 4")]
    t = d.get("team") or {}
    p = d.get("plan") or {}
    waiting = f"  ·  ⏳ {t['waiting_seat']} waiting for a seat" if t.get("waiting_seat") else ""
    package = (f"📘 {p['package_label']}: {p['cards']} awareness cards, {p['cards_per_week']} a week — sent "
               "automatically after each licence") if p else ""
    return [
        _section(f"*5 · Licences & awareness cards*\n🪪 *{t.get('licensed', 0)}* of {t.get('members', 0)} licensed"
                 f"{waiting}\n{package}"),
        _actions("aidl_step_licences",
                 _button("View team progress", "aidl_team_progress", value="team", style="primary"),
                 _button("📬 Send reference cards", "aidl_open_cards", value="cards") if "cards" in d["tabs"] else None,
                 _button("⬇ Export coverage", "aidl_open_home", value="home")),
    ]


def _home(d: dict) -> list:
    """Admin Center: welcome + the 5 setup steps, one after the other. Every
    feature lives inside its step (no tab bar), so the admin never has to go
    back to a home page."""
    st = d.get("setup") or _DEMO_SETUP
    name = d["admin_name"]
    done = st["done_count"]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🚦 AIDL Admin Center", "emoji": True}},
        _context(f"*{d['org_name']}*  ·  AI Driving License  ·  Signed in as *{name}*"),
        _section(f"👋 *Welcome, {name}.* Let's set up AI Driving License for *{d['org_name']}* — "
                 "a few short steps, top to bottom.\n" + "🟩" * done + "⬜" * (4 - done)
                 + f"  *{done} of 4 setup steps done*"),
        {"type": "divider"},
    ]
    if d.get("policy_only"):  # asked to create the AUP only
        return blocks + _step_aup(d, st)
    for step in (_step_aup, _step_admins, _step_team, _step_send, _step_licences):
        blocks += step(d, st)
        blocks.append({"type": "divider"})
    return blocks[:-1]


def _plan_field(d: dict) -> dict:
    p = d.get("plan")
    if not p:
        return _md(f"*Seats purchased*\n*{d['seats_purchased']}* seats  ·  _renews {d['seats_renew']}_")
    return _md(f"*{p['label']} plan*\n*{p['used']}* of {p['limit']} users  ·  "
               f"_{p['package_label']}: {p['cards']} cards, {p['cards_per_week']}/week_")


def _plan_notice(d: dict) -> list:
    p = d.get("plan")
    if not p or not p["full"]:
        return []
    return [_context(f"⛔ *Your {p['label']} plan is full* ({p['used']} of {p['limit']} users). People added to the "
                     "channel now are removed again automatically. Remove someone who hasn't earned a license, "
                     + ("or upgrade to *Basic*." if p["label"] == "Trial" else "or buy more seats."))]


def _team_progress_blocks(d: dict) -> list:
    """Admin progress view (summary): joined → accepted → licensed."""
    t = d.get("team")
    if not t:
        return []
    n = t["members"]
    licensed = f"*{t['licensed']}* of {n}"
    if t["waiting_seat"]:
        licensed += f"  ·  ⏳ {t['waiting_seat']} waiting for a seat"
    return [
        {"type": "section", "text": _md("👥  *TEAM PROGRESS*"),
         "accessory": _button("View team progress", "aidl_team_progress", value="team")},
        {"type": "section", "fields": [
            _md(f"*Joined*\n*{n}* member{'s' if n != 1 else ''}"),
            _md(f"*📖 Highway Code accepted*\n*{t['highway_code']}* of {n}"),
            _md(f"*🚦 Traffic Light accepted*\n*{t['traffic_light']}* of {n}"),
            _md(f"*🪪 Licensed*\n{licensed}"),
        ]},
    ]


def _aup_block(d: dict) -> dict:
    """AUP status — a red alert with Send reminders when people haven't signed."""
    if not d["policy"].get("has_answers"):
        return {"type": "section",
                "text": _md("📝 *Set up your AI policy*\nAnswer 8 quick questions and AIDL generates your team's "
                            "AI Acceptable Use Policy."),
                "accessory": _button("Answer policy questions", "aidl_policy_open", value="policy", style="primary")}
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
    order = ["add-admin", "cards", "ai-apps"]
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
    "cards": _cards,
    "ai-apps": lambda d: _apps(d, "ai"),
    "it-apps": lambda d: _apps(d, "it"),
}


def admin_card_blocks(d: dict, tab: str = "home", *, open_url: str = "") -> list:
    """Blocks for one Admin card. `open_url` (a private, signed link) is only
    passed for ephemeral messages that just the clicking admin can see."""
    if tab not in d["tabs"]:
        tab = "home"
    # No tab bar: Home is the step-by-step setup, and every other card is
    # opened from its step.
    if tab == "home":
        return _home(d) + [_context("AIDL · AI Driving License")]
    blocks = [_app_line(d["org_name"])]
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

    from .slack_onboarding import primary_admin

    # The shared channel card greets the admin who signed the organization up.
    primary = primary_admin(org) or user
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
        _context("Next: read your *AI Acceptable Use Policy*, then accept the *Highway Code* and the "
                 "*Traffic Light Check* to earn your Learner's Permit."),
    ]


def licence_progress_blocks(member, org) -> list:
    """License tab before the licence exists: what's left to earn it."""
    from .licensing import ACK_LABELS, LEARNER_REQUIREMENTS, acknowledged_items, seats_left

    done = acknowledged_items(member)
    lines = [f"{'✅' if item in done else '⬜'}  {ACK_LABELS[item]}" for item in LEARNER_REQUIREMENTS]
    blocks = [
        _context(f"*AIDL* · APP  for {org.name}"),
        {"type": "header", "text": {"type": "plain_text", "text": "🪪 Earn your Learner's Permit", "emoji": True}},
        _section("Accept both of these and your AI Driving License is issued automatically:\n" + "\n".join(lines)),
    ]
    if len(done) == len(LEARNER_REQUIREMENTS) and seats_left(org) <= 0:
        blocks.append(_context("⏳ All done — your license is waiting for a free seat. Your admin has been told."))
    else:
        blocks.append(_context("Use the tabs below to open each one."))
    return blocks


def user_license_blocks(member, org) -> list:
    from django.conf import settings

    if not member.licence_issued:
        return licence_progress_blocks(member, org)

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


def _ack_block(member, item: str) -> dict:
    """'I acknowledge' button, or when it was accepted."""
    from .licensing import acknowledged_at

    when = acknowledged_at(member, item)
    if when:
        return _context(f"✅ You accepted this on {when.strftime('%d %b %Y')}")
    return {"type": "actions", "block_id": f"aidl_ack_{item}", "elements": [
        _button("✅ I've read and accept", f"aidl_ack_{item}", value=item, style="primary")]}


def highway_code_blocks(org, member=None) -> list:
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
        _button("Open full Highway Code", "aidl_hc_full", value="full")]})
    if member is not None:
        blocks.append(_ack_block(member, "highway_code"))
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
    ("aup", "📄 AUP"),
    ("license", "🪪 License"),
    ("highway-code", "📖 Highway Code"),
    ("traffic-light", "🚦 Traffic Light Check"),
)


def user_traffic_light_blocks(org, member=None) -> list:
    """Traffic Light Check for the learner dashboard. The lights come from
    the organization's policy answers (not from an admin's selection)."""
    from .models import TrafficLightRating
    from .org_policy import get_answers, policy_effects
    from .slack_cards import TRAFFIC_LIGHTS

    fx = policy_effects(get_answers(org))
    emoji = {"green": "🟢", "amber": "🟡", "red": "🔴"}
    blocks = [
        _context(f"*AIDL* · APP  for {org.name}"),
        {"type": "header", "text": {"type": "plain_text", "text": "🚦 Traffic Light Check", "emoji": True}},
        _section("Before you paste anything into an AI, check the lights."),
    ]
    for light in TRAFFIC_LIGHTS:
        items = list(light["items"])
        if light["id"] == "red" and fx["red_includes_confidential"]:
            items.append("All confidential and customer data")
        blocks.append(_section(f"{emoji[light['id']]} *{light['label']}* — _{light['tagline']}_\n"
                               + "\n".join(f"• {i}" for i in items) + f"\n*→ {light['action']}*"))
    if member is not None:
        blocks.append(_ack_block(member, "traffic_light"))
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


def _aup_for(member, org) -> list:
    from .aup import generate_aup

    return aup_blocks(generate_aup(org), member)


def user_dashboard_blocks(member, org, tab: str = "license") -> list:
    """One message with the prototype's tab bar; clicking a tab swaps the
    card shown in the message (slack_interactions.py)."""
    content = {
        "home": lambda: user_welcome_blocks(member, org),
        "aup": lambda: _aup_for(member, org),
        "license": lambda: user_license_blocks(member, org),
        "highway-code": lambda: highway_code_blocks(org, member),
        "traffic-light": lambda: user_traffic_light_blocks(org, member),
    }.get(tab) or (lambda: user_license_blocks(member, org))
    item = {"aup": "aup", "highway-code": "highway_code", "traffic-light": "traffic_light"}.get(tab)
    if item and member is not None:
        from .admin_setup import ITEM_LABEL, visible_to

        if not visible_to(member, org, item):  # the admin hasn't sent this card yet
            content = lambda: [  # noqa: E731
                {"type": "header", "text": {"type": "plain_text", "text": ITEM_LABEL[item], "emoji": True}},
                _section(f"⏳ Your AIDL admin at *{org.name}* hasn't sent this yet — it will appear here "
                         "as soon as they do."),
            ]
    return content() + [{"type": "divider"}, user_tab_buttons(tab)]


# ---------- Generated AUP card (aup.py) ----------

def _app_lines(apps: list[dict]) -> str:
    return "\n".join(
        f"{a['dot']}  *{a['name']}*" + (f"  ·  _{a['category']}_" if a.get("category") and a["category"] != "Other" else "")
        + f"  —  {a['data']} data"
        for a in apps
    )


def aup_blocks(aup: dict, member=None) -> list:
    """The user's AI Acceptable Use Policy. With `member`, it's their personal
    copy with an accept button; without, it's the admin preview."""
    if member is not None and not aup.get("has_answers"):
        return [
            {"type": "header", "text": {"type": "plain_text", "text": "📄 Your AI Acceptable Use Policy", "emoji": True}},
            _section(f"⏳ *{aup['org_name']}* is still setting up its AI policy. Your AIDL admin will send it to you "
                     "here when it's ready."),
            _context("Meanwhile, start with the *Highway Code* and the *Traffic Light Check* to earn your Learner's Permit."),
        ]
    blocks = [
        _context(f"*📄 AIDL · Acceptable Use Policy*   |   {aup['org_name']}"),
        {"type": "header", "text": {"type": "plain_text", "text": "📄 Your AI Acceptable Use Policy", "emoji": True}},
        _context((f"Prepared for *{member.full_name or member.email}*  ·  " if member is not None else "Team version  ·  ")
                 + f"`{aup['version']}`  ·  {aup['generated']}"),
    ]
    if aup["status"] == "draft":
        blocks.append(_context("📝 *Draft* — your organization is still finalising its AI policy."))
    elif aup["status"] == "not_available":
        blocks.append(_context("ℹ️ Generated by AIDL from your organization's settings while its own written policy is prepared."))
    blocks.append(_section(f"These are the AI and IT apps you can use at *{aup['org_name']}*, the data each one "
                           "may handle, and the rules that go with them."))

    blocks.append({"type": "divider"})
    blocks.append(_section("*✅  AI APPS YOU CAN USE*\n" + (_app_lines(aup["allowed_ai"]) or "_None approved yet — ask your admin._")))
    blocks.append(_section("*✅  IT APPS YOU CAN USE*\n" + (_app_lines(aup["allowed_it"]) or "_None approved yet — ask your admin._")))
    blocks.append(_context("🟢 Public only   🟡 Internal   🔴 Internal + Confidential   ⚫ No data"))

    blocks.append({"type": "divider"})
    if aup["prohibited"]:
        blocks.append(_section("*⛔  DON'T USE*\n" + "\n".join(
            f"•  *{a['name']}*  ·  _{'AI' if a['kind'] == 'ai' else 'IT'} app_" for a in aup["prohibited"])))
    blocks.append(_section("*🚫  NEVER PUT INTO ANY AI TOOL*\n" + "\n".join(f"•  {item}" for item in aup["red_list"])))

    if aup["rules"]:
        blocks.append({"type": "divider"})
        blocks.append(_section("*📋  YOUR RULES*\n" + "\n".join(f"{i}.  {r}" for i, r in enumerate(aup["rules"], 1))))

    if member is not None:
        from .licensing import acknowledged_at

        when = acknowledged_at(member, "aup", aup["version"])
        blocks.append({"type": "divider"})
        if when:
            blocks.append(_context(f"✅ You accepted this policy on {when.strftime('%d %b %Y')}"))
        else:
            blocks.append({"type": "actions", "block_id": "aidl_ack_aup", "elements": [
                _button("✅ I've read and accept this policy", "aidl_ack_aup", value="aup", style="primary")]})
    else:
        blocks.append(_context("Each team member gets a personal copy and accepts it individually."))
    blocks.append(_context("Questions about this policy? Ask your AIDL admin."))
    return blocks
