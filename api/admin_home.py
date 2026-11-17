"""The AIDL Admin Center on the Slack app's Home tab (approved design: Direction C, guided, with the static
AIDL welcome banner — admin-center-directions.html).

Three surfaces:
  * App Home, per person (views.publish). Admins get the guided Admin Center drawn with *their own*
    permissions; learners get their licence progress. Rebuilt every time someone opens the tab
    (app_home_opened), and in the background whenever something changes (refresh()).
  * The shared #aidl message: once an organization's admins use the Home tab, it becomes a short post
    everyone in the channel can read (no admin buttons, no names). Until then it stays the full Admin Center
    (slack_blocks.admin_card_blocks), so nothing is lost while the Home tab isn't switched on in the Slack app.
  * Modals: every button opens the same popups as before (slack_interactions.py), and the server checks the
    clicking admin's permissions again on every click and submit.

The banner is one static file (api/static/aidl/admin-banner-<BANNER_VERSION>.png); everything that changes is
Block Kit text. Bump BANNER_VERSION when the picture changes, or Slack keeps its cached copy.
"""

from __future__ import annotations

import logging
import threading
import time

from django.conf import settings
from django.utils import timezone

from . import slack_client
from .models import AIDLUser, Organization

logger = logging.getLogger(__name__)

BANNER_VERSION = "v1"
CALLBACK = "aidl_home"
HOME_CHANNEL = "@home"        # payload["channel"] for clicks made on the Home tab (no channel there)
DM_REPLY = "aidl-dm:"         # response_url stand-in: replies to Home clicks go by direct message
MORE = "aidl_home_more"       # the "More" overflow menu; its option values are "<action_id>|<value>"
MAX_BLOCKS = 100              # Slack's limit for a Home tab

STEP_TITLES = {1: "Your AI policy", 2: "Add admins", 3: "Add your team", 4: "Approve cards", 5: "Start the programme"}
MARK = {"done": "✅", "skipped": "⏭️", "next": "👉", "ready": "◻️", "locked": "🔒"}


# ---------- small Block Kit helpers ----------

def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text[:3000]}


def _section(text: str, **extra) -> dict:
    return {"type": "section", "text": _md(text), **{k: v for k, v in extra.items() if v}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [_md(text)]}


def _button(text: str, action_id: str, value: str = "", *, style: str = "", url: str = "", confirm: dict | None = None):
    btn = {"type": "button", "text": {"type": "plain_text", "text": text[:75], "emoji": True}, "action_id": action_id}
    if value:
        btn["value"] = value
    if style:
        btn["style"] = style
    if url:
        btn["url"] = url
    if confirm:
        btn["confirm"] = confirm
    return btn


def _actions(block_id: str, *elements) -> dict | None:
    elements = [e for e in elements if e]
    return {"type": "actions", "block_id": block_id, "elements": elements} if elements else None


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _pct(a: int, b: int) -> int:
    return round(a / b * 100) if b else 0


def banner_url() -> str:
    return f"{settings.BACKEND_URL}/static/aidl/admin-banner-{BANNER_VERSION}.png"


def _banner() -> dict:
    return {"type": "image", "image_url": banner_url(), "alt_text": "AIDL Admin Center"}


def _updated() -> dict:
    ts = int(time.time())
    when = timezone.localtime().strftime("%d %b %Y, %H:%M")
    return _context(f"Updated <!date^{ts}^{{date_short_pretty}} at {{time}}|{when}>  ·  AIDL · AI Driving Licence")


# ---------- the setup journey ----------

def _master_name(d: dict) -> str:
    master = next((a for a in d.get("admins") or [] if a.get("primary")), None)
    return (master or {}).get("name") or "your master admin"


def _who(d: dict, user: AIDLUser) -> str:
    if d.get("is_primary"):
        return "the master admin"
    from .admin_perms import labels

    can = [label.lower() for label in labels(user)]
    return "an admin who can " + ", ".join(can) if can else "an admin (no permissions yet)"


def steps(d: dict) -> list[dict]:
    """The five setup steps with their status (done / skipped / next / ready / locked), using the same rules
    as admin_setup.summary and the channel Admin Center."""
    st, p = d["setup"], d.get("plan") or {}
    done = {1: st["aup_done"], 2: st["admins_done"], 3: st["team_done"], 4: st["approved_ready"], 5: st["cards_done"]}
    unlocked = {1: True, 2: done[1], 3: done[1] and done[2], 4: done[1],
                5: done[1] and done[3] and (done[4] or done[5])}
    nxt = next((n for n in range(1, 6) if not done[n] and unlocked[n]), 0)
    can = {1: d.get("can_policy"), 2: d.get("can_admins"), 3: d.get("can_team"), 4: d.get("can_approve_cards"),
           5: d.get("can_send")}
    after = {2: "step 1", 3: "steps 1 and 2", 4: "step 1", 5: "steps 3 and 4"}
    out = []
    for n in range(1, 6):
        if done[n]:
            status = "skipped" if n == 2 and d.get("admin_count", 1) <= 1 else "done"
        elif not unlocked[n]:
            status = "locked"
        else:
            status = "next" if n == nxt else "ready"
        out.append({"n": n, "title": STEP_TITLES[n], "status": status, "can": bool(can[n]),
                    "after": after.get(n, ""), "detail": _detail(n, status, d, st, p)})
    return out


def _detail(n: int, status: str, d: dict, st: dict, p: dict) -> str:
    members, limit = st["members"], p.get("limit", 0)
    if n == 1:
        if status != "done":
            return "Turn your AI policy into short cards your team agrees to. AIDL suggests the answers."
        cards = st.get("policy") or []
        later = sum(c["state"] != "sent" for c in cards)
        text = f"{len(cards)} of 14 policy cards approved"
        if st["cards_done"]:
            text += f" · {len(cards) - later} sent"
        return text + (f" · {later} scheduled" if later else "")
    if n == 2:
        if status == "skipped":
            return "Skipped. You can add admins at any time."
        if status == "done":
            return f"{d.get('admin_count', 0)} admins share the work"
        return "Optional. Invite HR, IT or Compliance colleagues and choose what each one can do."
    if n == 3:
        if status == "done":
            return f"{p.get('used', members)} of {limit} seats used"
        return f"Choose who earns an AI licence. Your {p.get('label', '')} plan has {limit} seats."
    if n == 4:
        a = st["approvals"]
        if status == "done":
            return f"Onboarding cards approved · {a['approved']} of {a['total']} awareness cards approved"
        return "Nothing reaches your team until you approve it. Preview every card first."
    if status == "done":
        return f"Running · {st.get('licensed', 0)} of {members} licensed"
    return "Everyone gets a welcome card, then the policy cards and two short checks. Licences are issued at the end."


def _step_buttons(step: dict, d: dict) -> list:
    st = d["setup"]
    n = step["n"]
    if n == 1:
        buttons = [_button("📌 Add a policy card", "aidl_policy_open", "policy", style="primary")]
        if st.get("aup_upload_allowed"):
            up = st.get("aup_upload") or {}
            buttons.append(_button("📋 Review uploaded cards", "aidl_aup_upload_review", "review")
                           if up.get("status") in ("ready", "processing")
                           else _button("📤 Upload your own AUP", "aidl_aup_upload_open", "upload"))
        return buttons
    if n == 2:
        return [_button("🧑‍💼 Add an admin", "aidl_open_add-admin", "add-admin", style="primary"),
                _button("Skip for now", "aidl_setup_skip", "admins")]
    if n == 3:
        return [_button("👥 Add people", "aidl_add_people", "team", style="primary")]
    if n == 4:
        return [_button("📋 Review cards", "aidl_cards_review", "1", style="primary")]
    members = st["members"]
    confirm = {"title": {"type": "plain_text", "text": "Start the programme?"},
               "text": {"type": "plain_text", "text": f"{_plural(members, 'person gets', 'people get')} the AIDL "
                                                      "welcome card now. People who join later start automatically."},
               "confirm": {"type": "plain_text", "text": "Start"}, "deny": {"type": "plain_text", "text": "Not yet"}}
    return [_button(f"📤 Start for {_plural(members, 'person', 'people')}", "aidl_send_item", "flow", style="primary",
                    confirm=confirm)]


def _upload_note(st: dict) -> dict | None:
    up = st.get("aup_upload") or {}
    if up.get("status") == "processing":
        return _context(f"⏳ AIDL is writing your 14 cards from _{up['file_name']}_. You'll get a message when "
                        "they're ready.")
    if up.get("status") == "failed":
        return _context(f"⚠️ AIDL couldn't write cards from _{up['file_name']}_. {up.get('error', '')}")
    return None


def _next_step(d: dict, all_steps: list[dict]) -> list:
    cur = next((s for s in all_steps if s["status"] == "next"), None)
    if cur is None:
        return []
    blocks = [_section(f"*Your next step*\n*{cur['n']}. {cur['title']}*\n{cur['detail']}")]
    if cur["can"]:
        blocks.append(_actions(f"aidl_home_next_{cur['n']}", *_step_buttons(cur, d)))
        if cur["n"] == 1:
            blocks.append(_upload_note(d["setup"]))
    else:
        blocks.append(_context(f"🔐 Your admin permissions don't cover this step. *{_master_name(d)}* (master admin) "
                               "can do it, or give you access."))
    return blocks


# ---------- things that need an admin (any time, not only during setup) ----------

def _attention(d: dict) -> list:
    st, p, t, a = d["setup"], d.get("plan") or {}, d.get("team") or {}, d.get("acks") or {}
    items = []
    upgrade = None
    if d.get("can_billing") and not d.get("testing_access") and p.get("can_upgrade"):
        upgrade = _button("⬆️ Upgrade plan", "aidl_upgrade", "upgrade")
    if p.get("full"):
        items.append(_section(f"⛔ *All {p['limit']} seats on your {p['label']} plan are in use.*\nPeople added to the "
                              "channel now are taken out again automatically.", accessory=upgrade))
    if t.get("waiting_seat"):
        items.append(_section(f"⏳ *{_plural(t['waiting_seat'], 'person has', 'people have')} finished but "
                              f"{'is' if t['waiting_seat'] == 1 else 'are'} waiting for a free seat* to get a licence.",
                              accessory=None if p.get("full") else upgrade))
    approvals = st["approvals"]
    if st["approved_ready"] and approvals["waiting"] and st["cards_done"]:
        items.append(_section(f"📋 *{_plural(approvals['waiting'], 'awareness card is', 'awareness cards are')} waiting "
                              "for approval.* They're postponed until someone approves them.",
                              accessory=_button("Review cards", "aidl_cards_review", "1")
                              if d.get("can_approve_cards") else None))
    up = st.get("aup_upload") or {}
    if up.get("status") == "ready" and st["aup_done"]:
        items.append(_section(f"📄 *Cards from _{up['file_name']}_ are ready to review.*",
                              accessory=_button("Review", "aidl_aup_upload_review", "review")
                              if d.get("can_policy") else None))
    late = a.get("overdue") or []
    if late:
        items.append(_section(f"⏰ *{_plural(len(late), 'person is', 'people are')} overdue on an awareness card* "
                              "(reminded 3+ days ago, still not acknowledged)."))
        for row in late[:5]:
            reminded = row["reminded_by_admin"]
            items.append(_section(f"{row['name']}  ·  _{row['title']}_" + ("  ·  ✅ reminded by an admin" if reminded else ""),
                                  accessory=None if reminded else _button("⏰ Remind", "aidl_ack_remind", row["user_id"])))
        if len(late) > 5:
            items.append(_context(f"…and {len(late) - 5} more in *Export coverage*."))
    if not items:
        return []
    return [_section("*Needs your attention*"), *items]


# ---------- the running programme ----------

def _programme(d: dict) -> list:
    st, t, a = d["setup"], d.get("team") or {}, d.get("acks") or {}
    if not st["cards_done"]:
        return []
    members = t.get("members", st["members"])
    agreed_all = st["sends"]["aup"]["accepted"]
    fields = [
        _md(f"*Licences issued*\n{t.get('licensed', 0)} of {_plural(members, 'person', 'people')}"),
        _md(f"*Policy cards*\n{t.get('policy_sent', 0)} of {t.get('policy_total', 14)} sent · "
            f"{agreed_all} of {members} agreed to all"),
        _md(f"*Highway Code*\n{t.get('highway_code', 0)} of {members} accepted"),
        _md(f"*Traffic Light Check*\n{t.get('traffic_light', 0)} of {members} accepted"),
    ]
    if a.get("sent"):
        fields += [_md(f"*Awareness cards sent*\n{a['sent']} deliveries"),
                   _md(f"*Acknowledged*\n{a['acked']} of {a['sent']} ({_pct(a['acked'], a['sent'])}%)")]
    blocks = [_section("*Your programme*"), {"type": "section", "fields": fields}]
    notes = []
    if a.get("sent"):
        notes.append("A delivery is one card sent to one person.")
        if a.get("month_sent"):
            notes.append(f"This month: {a['month_acked']} of {a['month_sent']} acknowledged "
                         f"({_pct(a['month_acked'], a['month_sent'])}%).")
        if a.get("best"):
            notes.append(f"Most acknowledged: _{a['best'][0]}_ {a['best'][1]}%"
                         + (f" · least: _{a['worst'][0]}_ {a['worst'][1]}%" if a.get("worst") else "") + ".")
    if notes:
        blocks.append(_context("  ".join(notes)))
    left = members - t.get("licensed", 0)
    buttons = []
    if left > 0 and d.get("can_send"):
        buttons.append(_button(f"🔔 Remind {_plural(left, 'person', 'people')} who haven't finished",
                               "aidl_remind_item", "flow"))
    if "home" in d.get("tabs", []):
        buttons.append(_button("⬇ Export coverage", "aidl_open_home", "home"))
    blocks.append(_actions("aidl_home_programme", *buttons))
    return blocks


# ---------- all steps, plan and manage ----------

def _all_steps(d: dict, all_steps: list[dict]) -> list:
    started = d["setup"]["cards_done"]
    blocks = [_section("*Setup*" if started else "*All steps*")]
    for s in all_steps:
        if s["status"] == "next":
            sub = "you are here"
        elif s["status"] == "locked":
            sub = f"opens after {s['after']}"
        elif s["status"] == "ready":
            sub = "ready whenever you are"
        else:
            sub = s["detail"]
        blocks.append(_context(f"{MARK[s['status']]}  *{s['n']}. {s['title']}*  ·  {sub}"))
    return blocks


def _plan_line(d: dict) -> dict | None:
    p, st = d.get("plan"), d["setup"]
    if not p:
        return None
    a = st["approvals"]
    logo = ("on your cards and licences" if d.get("has_logo") else "not added yet") if p.get("plan") != "trial" \
        else "Basic and Enterprise plans"
    return _context(f"Awareness cards: *{p['cards']} in your {p['label']} plan*, {a['approved']} approved  ·  "
                    f"{p['cadence']}  ·  Company logo: {logo}")


def _manage(d: dict, all_steps: list[dict]) -> list:
    st, p = d["setup"], d.get("plan") or {}
    done = {s["n"]: s["status"] in ("done", "skipped") for s in all_steps}
    main = [
        d.get("can_policy") and done[1] and _button("📌 Policy cards", "aidl_policy_open", "policy"),
        d.get("can_admins") and done[1] and _button("🧑‍💼 Admins", "aidl_open_add-admin", "add-admin"),
        d.get("can_team") and done[3] and _button("👥 Add people", "aidl_add_people", "team"),
        d.get("can_approve_cards") and done[1] and _button("📋 Card approval", "aidl_cards_review", "1"),
    ]
    more = [
        done[1] and ("📄 View policy", "aidl_aup_view|view"),
        d.get("can_policy") and st.get("company_set") and ("🏛 Your regulators", "aidl_regulators_open|regulators"),
        d.get("can_team") and done[3] and ("🚪 Remove people", "aidl_team_manage|team"),
        st["cards_done"] and ("🔔 Acknowledgement alerts", "aidl_ack_alerts|alerts"),
        d.get("can_admins") and p.get("plan") not in (None, "trial") and (
            "🖼️ Company logo", "aidl_logo_open|logo"),
        d.get("can_apps") and done[1] and ("🤖 AI apps", "aidl_tab_ai-apps|ai-apps"),
    ]
    more = [m for m in more if m][:5]
    elements = [b for b in main if b]
    if more:
        elements.append({"type": "overflow", "action_id": MORE, "options": [
            {"text": {"type": "plain_text", "text": label, "emoji": True}, "value": value} for label, value in more]})
    if not elements:
        return []
    return [_context("Change something later"), _actions("aidl_home_manage", *elements)]


def _overview(d: dict, user: AIDLUser, all_steps: list[dict], returning: bool) -> list:
    st, p = d["setup"], d.get("plan") or {}
    count = sum(s["status"] in ("done", "skipped") for s in all_steps)
    first = d.get("admin_name") or "there"
    greet = f"Welcome back, {first}" if returning else f"Welcome, {first}"
    seats = f"{p.get('used', st['members'])} of {p.get('limit', '—')} seats" + ("  ·  full" if p.get("full") else "")
    accessory = None
    if d.get("can_billing") and d.get("testing_access"):
        accessory = _button("⚙️ Change schedule", "aidl_promo_schedule", "schedule")
    elif d.get("can_billing") and p:
        accessory = _button("⬆️ Upgrade plan" if p.get("can_upgrade") else "⚙️ Change schedule", "aidl_upgrade", "upgrade")
    return [
        _banner(),
        {"type": "header", "text": {"type": "plain_text", "text": greet[:150], "emoji": True}},
        _section(f"You're managing AI Driving Licence for *{d['org_name']}* as {_who(d, user)}."),
        {"type": "section", "fields": [
            _md(f"*Organisation*\n{d['org_name']}"),
            _md(f"*Plan*\n{p.get('label', '—')}"),
            _md(f"*Team*\n{seats}"),
            _md(f"*Setup*\n{'●' * count}{'○' * (5 - count)}  {count} of 5 steps"),
        ], **({"accessory": accessory} if accessory else {})},
    ]


def admin_home_blocks(d: dict, user: AIDLUser, *, returning: bool = True) -> list:
    """The guided Admin Center for one admin (d = slack_cards.build_admin_cards_context(user)).
    `returning`: they have opened the Home tab before ("Welcome back")."""
    all_steps = steps(d)
    blocks = _overview(d, user, all_steps, returning) + [{"type": "divider"}]
    blocks += _next_step(d, all_steps)
    attention = _attention(d)
    programme = _programme(d)
    for part in (attention, programme):
        if part:
            if blocks[-1].get("type") != "divider":
                blocks.append({"type": "divider"})
            blocks += part
    blocks += [{"type": "divider"}, *_all_steps(d, all_steps), {"type": "divider"}, _plan_line(d),
               *_manage(d, all_steps), _updated()]
    blocks = [b for b in blocks if b]
    if len(blocks) > MAX_BLOCKS:  # never happens with today's content; keep the page publishable regardless
        blocks = blocks[:MAX_BLOCKS - 1] + [_updated()]
    return blocks


# ---------- learners' Home tab ----------

def employee_home_blocks(member: AIDLUser, org: Organization) -> list:
    from . import admin_setup
    from .aup_store import points_accepted, required_points
    from .licensing import ACK_LABELS, LEARNER_REQUIREMENTS, acknowledged_items, licence_status

    blocks = [_context(f"*AIDL*  ·  AI Driving Licence for *{org.name}*"),
              {"type": "header", "text": {"type": "plain_text", "text": "Your AI Driving Licence", "emoji": True}}]
    if member.licence_issued:
        until = member.licence_expires_at.strftime("%d %b %Y") if member.licence_expires_at else "—"
        blocks += [
            {"type": "section", "fields": [_md(f"*Licence*\n`{member.licence_number}`"),
                                           _md(f"*Status*\n{licence_status(member).title()}"),
                                           _md(f"*Class*\nLearner (L)"), _md(f"*Valid until*\n{until}")]},
            _context("Your licence card, with Download and Share, is in the *Messages* tab. New awareness cards "
                     "arrive there too."),
        ]
    elif not admin_setup.started(org):
        blocks.append(_section(f"*{org.name}* is getting AIDL ready. Your first card will arrive in the *Messages* "
                               "tab here. There's nothing for you to do yet."))
    else:
        done = acknowledged_items(member)
        lines = [f"{'✅' if item in done else '⬜'}  {ACK_LABELS[item]}" for item in LEARNER_REQUIREMENTS]
        total = len(required_points(org))
        if total:
            agreed = len(points_accepted(member, org))
            lines.append(f"{'✅' if agreed >= total else '⬜'}  📄 Policy cards: {agreed} of {total} agreed")
        blocks += [_section("*To earn your licence*\n" + "\n".join(lines)),
                   _context("Your cards are in the *Messages* tab. Press the button on each one.")]
    blocks.append(_actions("aidl_home_employee", _button("☰ Menu", "aidl_employee_menu", "menu")))
    blocks.append(_updated())
    return blocks


def _not_set_up_blocks() -> list:
    return [_context("*AIDL*  ·  AI Driving Licence"),
            _section("AIDL isn't set up for you in this workspace yet. When your organization adds you to its AIDL "
                     "programme, your cards and your licence appear here.")]


# ---------- the shared channel post ----------

def channel_blocks(org: Organization) -> list:
    from . import admin_setup

    learners = admin_setup._slack_learners(org)
    members = learners.count()
    if not admin_setup.started(org):
        return [
            _section(f"👋 *This is the AI Driving Licence channel for {org.name}.*\nAIDL is being set up. When it starts "
                     "you'll get, by direct message from AIDL:\n1. A welcome card\n2. Your company's AI policy cards "
                     "and two short checks\n3. Your AI Driving Licence"),
            _context("Admins: open *AIDL* in your Slack sidebar and choose *Home* for the Admin Center."),
        ]
    licensed = learners.filter(licence_issued=True).count()
    link = app_link(org)
    return [
        _section(f"🪪 *The AI Driving Licence programme is running at {org.name}.*\n{licensed} of "
                 f"{_plural(members, 'person has', 'people have')} their licence. Not yet? Your next card is waiting "
                 "in your direct messages from AIDL.",
                 accessory=_button("Open AIDL", "aidl_link_open", url=link) if link else None),
        _context("Admins: the Admin Center is on the AIDL app's *Home* tab."),
    ]


def app_link(org: Organization) -> str:
    from .admin_setup import state

    app_id = state(org).get("slack_app_id", "")
    if not (app_id and org.slack_team_id):
        return ""
    return f"https://slack.com/app_redirect?app={app_id}&team={org.slack_team_id}"


def home_confirmed(org: Organization) -> bool:
    """An admin of this organization has opened AIDL's Home tab, so the Slack app has it switched on."""
    from .admin_setup import state

    return bool(state(org).get("app_home_at"))


def post_channel(org: Organization, fallback: AIDLUser | None = None) -> bool:
    """The shared #aidl message: the short post once the Home tab is in use, else the full Admin Center
    (drawn for the master admin, as before)."""
    with _org_lock(org):
        if home_confirmed(org):
            return slack_client.post_admin_center(org, channel_blocks(org), f"AIDL · {org.name}")
        from .slack_blocks import admin_card_blocks, admin_card_text
        from .slack_cards import build_admin_cards_context
        from .slack_onboarding import primary_admin

        admin = primary_admin(org) or fallback
        if admin is None:
            return False
        data = build_admin_cards_context(admin)
        return slack_client.post_admin_center(org, admin_card_blocks(data, "home"), admin_card_text(data))


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _org_lock(org: Organization) -> threading.Lock:
    """One channel post at a time per organization (two at once could both post a new message)."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(org.pk), threading.Lock())


# ---------- publishing the Home tab ----------

def _slack_id(user: AIDLUser) -> str:
    return user.microsoft_id.split(":")[-1] if (user.microsoft_id or "").startswith("slack:") else ""


def _publish(org: Organization, slack_user: str, blocks: list) -> bool:
    from .slack_blocks import has_images, text_fallback

    token = slack_client.bot_token(org)
    if not (token and slack_user):
        return False
    view = {"type": "home", "callback_id": CALLBACK, "blocks": blocks}
    result = slack_client.slack_api("views.publish", token, json={"user_id": slack_user, "view": view})
    if not result.get("ok") and result.get("error") in ("invalid_blocks", "downloading_image_failed") \
            and has_images(blocks):
        # Slack couldn't fetch the banner (e.g. a local backend): same page without it.
        result = slack_client.slack_api("views.publish", token, json={
            "user_id": slack_user, "view": {**view, "blocks": text_fallback(blocks)}})
    if not result.get("ok"):
        logger.warning("views.publish for %s: %s %s", org.pk, result.get("error"), result.get("response_metadata"))
    return bool(result.get("ok"))


def _is_admin_of(user: AIDLUser | None, org: Organization) -> bool:
    return (user is not None and user.is_active and user.role == AIDLUser.Role.ADMIN
            and user.organization_id == str(org.pk))


def _visited(org: Organization, slack_user: str) -> bool:
    from .admin_setup import state

    return slack_user in (state(org).get("home_users") or [])


def publish_for(org: Organization, user: AIDLUser, *, returning: bool | None = None) -> bool:
    """Draw this person's Home tab from the database as it is now."""
    from .slack_cards import build_admin_cards_context

    slack_user = _slack_id(user)
    if not slack_user:
        return False
    if _is_admin_of(user, org):
        returning = _visited(org, slack_user) if returning is None else returning
        return _publish(org, slack_user, admin_home_blocks(build_admin_cards_context(user), user, returning=returning))
    if user.organization_id == str(org.pk) and user.is_active:
        return _publish(org, slack_user, employee_home_blocks(user, org))
    return _publish(org, slack_user, _not_set_up_blocks())


def _admins(org: Organization):
    return AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True,
                                   microsoft_id__startswith=f"slack:{org.slack_team_id}:")


def publish_admin_homes(org: Organization) -> int:
    """Redraw every admin's Home tab. Each admin sees only what their own permissions allow."""
    count = 0
    for admin in _admins(org):
        try:
            count += publish_for(org, admin)
        except Exception:  # noqa: BLE001 — one admin's page must not stop the others
            logger.exception("Home tab for admin %s", admin.pk)
    return count


# ---------- keeping it current ----------

REFRESH_DELAY = 4  # seconds: gathers a burst of changes (e.g. 30 people joining at once) into one redraw
_PENDING: dict[str, bool] = {}
_PENDING_LOCK = threading.Lock()


def refresh_homes(org: Organization) -> None:
    """Redraw the admins' Home tabs soon, in the background. Calls that arrive while a redraw is waiting or
    running are folded into one more redraw, so a burst of events never queues dozens of rebuilds."""
    if not home_confirmed(org):
        return  # nobody uses the Home tab here yet; the first visit draws it
    _schedule(str(org.pk), channel=False)


def refresh(org: Organization) -> None:
    """Something changed outside an admin click (people joined or left, a licence was issued): redraw the
    shared channel post and the admins' Home tabs, coalesced and in the background."""
    _schedule(str(org.pk), channel=True)


def _schedule(key: str, *, channel: bool) -> None:
    if settings.SLACK_REPLY_SYNC:  # tests and scripts: at once
        _run(key, channel)
        return
    with _PENDING_LOCK:
        if key in _PENDING:
            _PENDING[key] = _PENDING[key] or channel
            return
        _PENDING[key] = channel
    threading.Thread(target=_worker, args=(key,), daemon=True).start()


def _worker(key: str) -> None:
    from django.db import close_old_connections

    try:
        time.sleep(REFRESH_DELAY)
        while True:
            with _PENDING_LOCK:
                channel = _PENDING.get(key, False)
                _PENDING[key] = None  # running; a call from now on asks for one more round
            try:
                _run(key, bool(channel))
            except Exception:  # noqa: BLE001
                logger.exception("Admin Center refresh for %s", key)
            with _PENDING_LOCK:
                if _PENDING.get(key) is None:
                    _PENDING.pop(key, None)
                    return
            time.sleep(REFRESH_DELAY)
    finally:
        close_old_connections()


def _run(key: str, channel: bool) -> None:
    org = Organization.objects.filter(pk=key, is_active=True).first()
    if org is None:
        return
    if channel:
        post_channel(org)
    if home_confirmed(org):
        publish_admin_homes(org)


# ---------- events ----------

_OPENED: dict[str, float] = {}
OPEN_THROTTLE = 3  # seconds: Slack sends app_home_opened again on every click on the tab


def on_home_opened(team_id: str, slack_user: str, app_id: str = "") -> None:
    """Someone opened AIDL's Home tab: draw it for them from the current data. The first admin visit also
    turns the shared channel message into the short post (the Home tab evidently works)."""
    from . import admin_setup

    now = time.monotonic()
    key = f"{team_id}:{slack_user}"
    if _OPENED.get(key, 0) > now:
        return
    _OPENED[key] = now + OPEN_THROTTLE
    org = Organization.objects.filter(slack_team_id=team_id, is_active=True).first()
    if org is None:
        return
    user = AIDLUser.objects.filter(microsoft_id=f"slack:{team_id}:{slack_user}", is_active=True).first()
    first_visit = returning = False
    if _is_admin_of(user, org):
        st = admin_setup.state(org)
        seen = list(st.get("home_users") or [])
        returning = slack_user in seen
        changes = {}
        if not st.get("app_home_at"):
            changes["app_home_at"] = timezone.now().isoformat()
            first_visit = True
        if not returning:
            changes["home_users"] = seen + [slack_user]
        if app_id and st.get("slack_app_id") != app_id:
            changes["slack_app_id"] = app_id
        if changes:
            org.refresh_from_db()
            admin_setup.update(org, **changes)
    if user is None:
        _publish(org, slack_user, _not_set_up_blocks())
    else:
        publish_for(org, user, returning=returning)
    if first_visit:
        post_channel(org)
