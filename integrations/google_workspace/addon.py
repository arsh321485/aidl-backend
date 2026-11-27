"""AIDL in Gmail's side panel: a Google Workspace add-on (HTTP runtime).

Google POSTs a JSON event to these endpoints and shows the card we answer with:
    POST /api/google-workspace/addon/         the homepage (the AIDL icon in Gmail's side panel)
    POST /api/google-workspace/addon/action/  a card button (parameter "do" says which)

Admins get the Admin Center (approve the starter cards, start, the team with
reminders, invites, CSV); employees get their cards (policy, Traffic Light Check,
Highway Code, awareness) and their licence, all as native Google cards. The
buttons do exactly what the AIDL web page's buttons do (ops.py).

Requests are checked with addon_auth.verify (Google-signed tokens). The add-on
deployment (Google Cloud > Workspace Marketplace SDK > HTTP deployments) is
described in addon_deployment.json.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from api.models import AIDLUser, Organization
from integrations.moodle import flow

from . import accounts, links, mail, ops, progress
from .addon_auth import AddonAuthError, AddonUser, endpoint, verify
from .google_auth import GoogleError, GoogleProfile

logger = logging.getLogger(__name__)

TEAM_ROWS = 40  # people listed in the side panel (the CSV has everyone)


# ---------- card pieces (Google Workspace card JSON) ----------

# The card headers' logo: the AIDL disc cropped tight on a transparent background
# (static/google_workspace/aidl-icon.png), inside the card as a data: image so Gmail never has to
# fetch it (a fetch can fail, e.g. behind a free ngrok tunnel). GOOGLE_ADDON_LOGO_URL overrides it.
ICON_FILE = Path(__file__).resolve().parent / "static" / "google_workspace" / "aidl-icon.png"
HEADER_PX = 96  # shown at ~40px; 96px stays sharp on high-density screens and keeps each card small


@lru_cache(maxsize=1)
def _icon_data_url() -> str:
    import base64
    import io

    from PIL import Image

    img = Image.open(ICON_FILE).convert("RGBA").resize((HEADER_PX, HEADER_PX), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def _logo() -> str:
    return os.environ.get("GOOGLE_ADDON_LOGO_URL") or _icon_data_url()


def _action_url() -> str:
    return endpoint(reverse("google-workspace-addon-action"))


def _button(text: str, do: str = "", *, url: str = "", filled: bool = False, disabled: bool = False,
            **params) -> dict:
    if url:
        on_click = {"openLink": {"url": url}}
    else:
        parameters = [{"key": "do", "value": do}] + [{"key": k, "value": str(v)} for k, v in params.items()]
        on_click = {"action": {"function": _action_url(), "parameters": parameters}}
    button = {"text": text, "onClick": on_click}
    if filled:
        button["color"] = {"red": 1, "green": 0.8, "blue": 0, "alpha": 1}
    if disabled:
        button["disabled"] = True
    return button


def _buttons(*buttons: dict | None) -> dict:
    return {"buttonList": {"buttons": [b for b in buttons if b]}}


def _text(html: str) -> dict:
    return {"textParagraph": {"text": html}}


def _image(url: str, alt: str) -> dict:
    return {"image": {"imageUrl": url, "altText": alt}}


def _row(text: str, bottom: str = "", top: str = "", button: dict | None = None) -> dict:
    row = {"text": text}
    if top:
        row["topLabel"] = top
    if bottom:
        row["bottomLabel"] = bottom
    if button:
        row["button"] = button
    return {"decoratedText": row}


def _section(widgets: list, header: str = "") -> dict:
    section = {"widgets": [w for w in widgets if w]}
    if header:
        section["header"] = header
    return section


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _card(title: str, subtitle: str, sections: list[dict], notice: str = "") -> dict:
    if notice:
        sections = [_section([_text(f"<b>{_esc(notice)}</b>")])] + sections
    return {"header": {"title": title, "subtitle": subtitle, "imageUrl": _logo(), "imageType": "SQUARE"},
            "sections": [s for s in sections if s.get("widgets")]}


LICENCE_WIDTH = 640  # the side panel is ~300px wide; this stays sharp and keeps the card small


def _licence_picture(user: AIDLUser, org: Organization) -> str:
    """The licence as a data: image inside the card, so Gmail shows it without fetching a URL
    (a fetch can fail, e.g. behind a free ngrok tunnel). "" = use the picture's URL instead."""
    import base64
    import io

    from PIL import Image

    from api import licence_image

    try:
        img = Image.open(io.BytesIO(licence_image.png_for(user, org))).convert("RGB")
        if img.width > LICENCE_WIDTH:
            img = img.resize((LICENCE_WIDTH, round(img.height * LICENCE_WIDTH / img.width)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85, optimize=True)
    except Exception:  # noqa: BLE001
        logger.exception("gmail add-on: licence picture for %s", user.pk)
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def _refresh() -> dict:
    return _button("↻ Refresh", "refresh")


# ---------- the cards ----------

def message_card(title: str, text: str, *buttons: dict, subtitle: str = "AI Driving Licence") -> dict:
    return _card(title, subtitle, [_section([_text(_esc(text)), _buttons(*buttons) if buttons else None])])


def setup_card(domain: str) -> dict:
    """Nobody at this company has set up AIDL yet: the website sign-up makes them its admin."""
    login = endpoint(reverse("google-workspace-login")) + "?redirect=1"
    return message_card(
        "Set up AIDL for your company",
        f"AIDL isn't set up for {domain or 'your company'} yet. The person who sets it up becomes the AIDL admin: "
        "they approve the starter cards and invite the team. Then everyone's cards and licence show up right "
        "here in Gmail.",
        _button("Set up AIDL with Google", url=login, filled=True), _refresh())


def admin_card(user: AIDLUser, org: Organization, notice: str = "", extra: list | None = None) -> dict:
    """`extra`: sections shown first (e.g. "Use this file as our AI policy" on an email with one attached)."""
    extra = extra or []
    t = flow.teacher_context(org)
    setup = []
    for c in t["onboarding"]:
        done = c["status"] == "approved"
        status = (f"✅ Approved{' by ' + _esc(c['by']) if c.get('by') else ''}" if done
                  else "Waiting for your approval")
        setup.append(_row(f"<b>{_esc(c['label'])}</b>", status,
                          button=None if done else _button("Approve", "approve", item=c["item"], filled=True)))
    if t["started"]:
        setup.append(_row("<b>🚀 Start the programme</b>", "✅ Started"))
    else:
        setup.append(_row("<b>🚀 Start the programme</b>",
                          "Ready to start" if t["ready"] else "Approve both cards first",
                          button=_button("Start", "start", filled=True, disabled=not t["ready"])))

    team = progress.rows(org)
    s = progress.summary(team)
    people = [_text(f"<b>{s['licensed']}</b> licensed · <b>{s['in_progress']}</b> in progress · "
                    f"<b>{s['invited']}</b> invited, not joined yet")]
    for r in team[:TEAM_ROWS]:
        people.append(_row(f"<b>{_esc(r['name'])}</b>", _esc(r["status"]),
                           top=_esc(r["email"]) if r["name"] != r["email"] else "",
                           button=_button("Remind", "remind", who=r["key"]) if r["can_remind"] else None))
    if len(team) > TEAM_ROWS:
        people.append(_text(f"…and {len(team) - TEAM_ROWS} more in the CSV."))
    if not team:
        people.append(_text("Nobody yet. Invite your colleagues below and they'll show up here."))
    export = links.link(user, reverse("google-workspace-export"))
    people.append(_buttons(
        _button(f"🔔 Remind everyone ({s['remindable']})", "remind", who="all") if s["remindable"] else None,
        _button("⬇️ Download CSV", url=export)))

    domain = user.email.rsplit("@", 1)[-1] if "@" in user.email else ""
    invite = [
        _text(f"Colleagues' work emails (only @{_esc(domain)}). Each one gets an email to join with Google."),
        {"textInput": {"name": "emails", "label": "Work emails", "type": "MULTIPLE_LINE",
                       "hintText": f"ben@{domain}, carla@{domain}"}},
        _buttons(_button("✉️ Send invites", "invite", filled=True)),
        _text("Or invite from your company's Google directory:"),
        _buttons(_button("✉️ Invite everyone at the company", "dir_invite_all", filled=True)),
        _buttons(_button("👥 Choose people", "dir_open")),
    ]
    from . import addon_policy

    return _card("AIDL Admin Center", f"{org.name} · Admin", extra + [
        _section(setup, "Get your team started"),
        _section(addon_policy.section(user, org), "Your AI policy"),
        _section(people, "Your team"),
        _section(invite, "Invite your team"),
        _section(_plan_rows(user, org), "Your plan"),
        _section([_buttons(_refresh())]),
    ], notice)


def _plan_rows(user: AIDLUser, org: Organization) -> list:
    """The plan and its licence seats. Paying happens on AIDL's plans page (not possible inside Gmail)."""
    from api import trial
    from api.licensing import seats_left
    from api.plans import plan_of, user_limit

    limit = user_limit(org)
    used = max(limit - seats_left(org), 0)
    seats = "Unlimited licences" if limit >= 100_000 else f"{used} of {limit} licences used"
    rows = [_row(f"<b>{_esc(plan_of(org)['label'])}</b>", trial.line(org) or seats,
                 top=seats if trial.line(org) else "")]
    plans_url = links.link(user, reverse("google-workspace-plans"))
    rows.append(_buttons(_button("⬆️ See plans & upgrade", url=plans_url)))
    rows.append(_text('<font color="#5E5843">Payment opens AIDL\'s secure plans page in a new tab.</font>'))
    return rows


def learner_card(user: AIDLUser, org: Organization, notice: str = "") -> dict:
    from api import admin_setup

    subtitle = org.name
    if not admin_setup.started(org):
        return message_card("Nothing to do yet", f"{org.name}'s AIDL admin is still setting things up. "
                            "Check back here a little later.", _refresh(), subtitle=subtitle)
    c = flow.learner_context(user, org)
    if "blocked" in c:
        return message_card(*c["blocked"], _refresh(), subtitle=subtitle)

    if c.get("welcome"):
        route = ["🚦 <b>Traffic Light Check</b> · what's safe to paste",
                 "📖 <b>The Highway Code</b> · 5 everyday rules"]
        if c.get("policy_count"):
            route.insert(0, f"📜 <b>{_esc(org.name)} policy</b> · {c['policy_count']} short cards to agree to")
        avatar = None if user.avatar_data else _button("🎨 Create my avatar", url=c["avatar_url"])
        return _card(f"👋 Hi {_esc(c['first'])}, welcome", subtitle, [_section([
            _text("AIDL helps you use AI safely and confidently, one short card at a time. "
                  "About 10 minutes, and your AI Driving Licence shows up right here."),
            _text("<br>".join(route)),
            _buttons(_button("Let's go →", "start_learning", filled=True), avatar),
        ])], notice)

    sections = []
    lic = c.get("licence")
    if lic:
        until = f", valid till <b>{user.licence_expires_at:%d %B %Y}</b>" if user.licence_expires_at else ""
        sections.append(_section([
            _text(f"🎉 You're road-ready, {_esc(c['first'])}! Here's your AI Driving Licence{until}."),
            _image(_licence_picture(user, org) or lic["image"], f"AI Driving Licence {user.licence_number}"),            _buttons(_button("📥 Download", url=lic["download"], filled=True),
                     _button("🔗 Share on LinkedIn", url=lic["share"]),
                     _button("🎨 Change avatar (once)", url=c["avatar_url"]) if c.get("can_change_avatar") else None),
        ], "Your licence"))
    policy = c.get("policy") or []
    if len(policy) > 1:
        # One click for everyone who doesn't want to press "I agree" on every card. It carries
        # the versions on screen, so a card changed meanwhile isn't agreed to unseen.
        shown = json.dumps([[p["n"], p["version"]] for p in policy])
        sections.append(_section([
            _text(f"Read them all? Agree to all <b>{len(policy)}</b> cards below in one go, "
                  "or agree to each card on its own."),
            _buttons(_button(f"✅ I agree to all {len(policy)} cards", "agree_all", cards=shown, filled=True)),
        ], "📜 Your policy cards"))
    for p in policy:
        sections.append(_section([
            _image(p["image"], f"Policy card: {p['title']}"),
            _buttons(_button("✅ I agree", "agree", n=p["n"], version=p["version"], filled=True)),
            _text('<font color="#5E5843">Your agreement is time-stamped and saved to your AIDL record.</font>'),
        ], f"Policy card · {p['icon']} {_esc(p['title'])}"))
    if c.get("waiting"):
        sections.append(_section([_text("⏳ <b>Your next card is on its way.</b> Your AIDL admin is approving it."),
                                  _buttons(_refresh())]))
    elif c.get("traffic"):
        sections.append(_section([_image(c["traffic"], "Traffic Light Check"),
                                  _buttons(_button("Got it. Next: The Highway Code →", "traffic_light", filled=True))],
                                 "🚦 Traffic Light Check"))
    elif c.get("highway"):
        sections.append(_section([_image(c["highway"], "The Highway Code"),
                                  _buttons(_button("Got it. Get my licence →", "highway_code", filled=True))],
                                 "📖 The Highway Code"))
    elif c.get("stage") == "licence" and not user.licence_issued:
        sections.append(_section([_text(f"⏳ <b>Almost there.</b> All licence seats on {_esc(org.name)}'s plan "
                                        "are in use, so your licence is issued as soon as one is free."),
                                  _buttons(_refresh())]))
    elif c.get("stage") == "licence" and not user.avatar_data:
        sections.append(_section([
            _text(f"You've earned your AI Driving Licence, {_esc(c['first'])}! Create the avatar that goes on it, "
                  "then press Refresh."),
            _buttons(_button("🎨 Create my avatar", url=c["avatar_url"], filled=True), _refresh())],
            "🎨 One last step"))
    for a in c.get("awareness") or []:
        sections.append(_section([_image(a["image"], a["title"]),
                                  _buttons(_button("✅ Got it", "paw", id=a["id"], filled=True))],
                                 f"AI awareness · {a.get('icon', '')} {_esc(a['title'])}"))
    if not sections:
        sections.append(_section([_text("✅ You're all caught up. New cards show up here."), _buttons(_refresh())]))
    return _card("My AI Driving Licence", subtitle, sections, notice)


def home_card(user: AIDLUser, org: Organization, notice: str = "", event: dict | None = None) -> dict:
    """`event`: the side-panel request; on an open email (event["gmail"]) an admin on a paid plan is
    offered its policy attachment first."""
    if user.role == AIDLUser.Role.ADMIN:
        from . import addon_policy

        offer = addon_policy.upload_offer(event, user, org) if (event or {}).get("gmail") else []
        return admin_card(user, org, notice, extra=offer)
    mail.send_next(user, org)  # e.g. back from making their avatar: the licence email
    return learner_card(user, org, notice)


# ---------- who is it ----------

def _person(who: AddonUser) -> tuple[AIDLUser | None, Organization | None, dict | None]:
    """(user, org, None), or (None, None, card) to show instead."""
    first, _, last = (who.name or "").partition(" ")
    profile = GoogleProfile(sub=who.sub, email=who.email, email_verified=who.email_verified, hd=who.hd,
                            first_name=first, last_name=last, full_name=who.name, picture="")
    if accounts.find_user(profile) is None and (not who.hd or accounts.organization_for_domain(who.hd) is None):
        # Nobody from this company is in AIDL yet. Admins often install add-ons for the whole
        # company, so the first person to open the panel must not silently become the AIDL admin:
        # setting up goes through the website sign-up.
        return None, None, setup_card(who.hd)
    try:
        user, org = accounts.sign_in(profile)
    except GoogleError as exc:
        return None, None, message_card("Can't open AIDL", str(exc))
    return user, org, None


def _verified(request, path: str):
    try:
        event = json.loads(request.body or b"{}")
    except ValueError:
        event = {}
    try:
        who = verify(event, path)
    except AddonAuthError as exc:
        logger.warning("gmail add-on request refused: %s", exc)
        return None, None, JsonResponse({"error": "unauthorized"}, status=403)
    return event, who, None


def _push(card: dict) -> JsonResponse:
    return JsonResponse({"action": {"navigations": [{"pushCard": card}]}})


def _update(card: dict, notice: str = "") -> JsonResponse:
    action = {"navigations": [{"updateCard": card}]}
    if notice:
        action["notification"] = {"text": notice[:300]}
    return JsonResponse({"renderActions": {"action": action}})


def _navigate(*steps: dict, notice: str = "") -> JsonResponse:
    action = {"navigations": list(steps)}
    if notice:
        action["notification"] = {"text": notice[:300]}
    return JsonResponse({"renderActions": {"action": action}})


def _form(event: dict) -> dict[str, list[str]]:
    """Every input on the card: {name: [values]} (tick boxes have several)."""
    common = event.get("commonEventObject") or {}
    return {name: list(((value or {}).get("stringInputs") or {}).get("value") or [])
            for name, value in (common.get("formInputs") or {}).items()}


def _params(event: dict) -> dict:
    common = event.get("commonEventObject") or {}
    params = dict(common.get("parameters") or {})
    for name, values in _form(event).items():
        params[name] = values[0] if values else ""
    return params


# ---------- endpoints ----------

@csrf_exempt  # Google calls these; each request is checked with Google-signed tokens instead
@require_POST
def home(request):
    _event, who, error = _verified(request, reverse("google-workspace-addon"))
    if error:
        return error
    user, org, card = _person(who)
    return _push(card or home_card(user, org))


@csrf_exempt
@require_POST
def message(request):
    """An email is open in Gmail (contextual trigger): the same AIDL, plus, for admins on a paid plan,
    "Use this file as our AI policy" when the email has a policy file attached."""
    event, who, error = _verified(request, reverse("google-workspace-addon-message"))
    if error:
        return error
    user, org, card = _person(who)
    return _push(card or home_card(user, org, event=event))


ADMIN_ONLY = {"policy_open", "policy_preview", "policy_approve", "policy_upload", "policy_publish",
              "dir_open", "dir_invite", "dir_invite_all"}


def _admin_action(event: dict, user: AIDLUser, org: Organization, do: str, p: dict) -> JsonResponse:
    """The admin's policy and directory buttons (cards that open on top of the Admin Center)."""
    from . import addon_directory, addon_policy

    root = {"popToRoot": True}
    if do == "policy_open" or do == "policy_preview":
        n = int(p.get("n") or 0)
        if n not in addon_policy.pc.TOPICS or addon_policy.pc.is_locked(org, n):
            return _update(home_card(user, org, event=event), "That topic is part of the Basic and Enterprise plans.")
        if do == "policy_open":
            return _navigate({"pushCard": addon_policy.topic_card(org, n, addon_policy.answers(org))})
        a = addon_policy.read_form(org, n, _form(event), p)
        addon_policy.aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
        return _navigate({"updateCard": addon_policy.topic_card(org, n, a)})
    if do == "policy_approve":
        n = int(p.get("n") or 0)
        if n not in addon_policy.pc.TOPICS or addon_policy.pc.is_locked(org, n):
            return _update(home_card(user, org, event=event), "That topic is part of the Basic and Enterprise plans.")
        a = addon_policy.read_form(org, n, _form(event), p)
        problem = addon_policy.approve(user, org, n, a)
        if problem:
            return _navigate({"updateCard": addon_policy.topic_card(org, n, a, problem)}, notice=problem)
        done = f"✅ “{addon_policy.pc.SHORT[n]}” is in your policy."
        return _navigate(root, {"updateCard": home_card(user, org, done, event)}, notice=done)
    if do == "policy_upload":
        notice = addon_policy.upload(event, user, org, p.get("att", ""), p.get("name", "policy"))
        return _update(home_card(user, org, notice, event), notice)
    if do == "policy_publish":
        notice = addon_policy.publish(user, org, p.get("upload", ""))
        return _update(home_card(user, org, notice, event), notice)
    if do in ("dir_open", "dir_invite_all") and addon_directory.missing_scope(event, user, org):
        # Google's granular consent: ask Google to show its own "Allow" screen for the directory permission.
        return JsonResponse({"requesting_google_scopes": {"scopes": [addon_directory.DIRECTORY_SCOPE]}})
    if do == "dir_open":
        card = addon_directory.card(event, user, org, p.get("select", "all"))
        return _navigate({"updateCard": card} if p.get("select") else {"pushCard": card})
    if do == "dir_invite_all":
        notice = addon_directory.invite_everyone(event, user, org)
        return _navigate(root, {"updateCard": home_card(user, org, notice, event)}, notice=notice)
    # dir_invite
    chosen = _form(event).get("people") or []
    notice = ops.invite(user, org, ",".join(chosen)) if chosen else "Tick at least one person first."
    if not chosen:
        return _navigate({"updateCard": addon_directory.card(event, user, org, "none", notice)}, notice=notice)
    return _navigate(root, {"updateCard": home_card(user, org, notice, event)}, notice=notice)


@csrf_exempt
@require_POST
def action(request):
    event, who, error = _verified(request, reverse("google-workspace-addon-action"))
    if error:
        return error
    user, org, card = _person(who)
    if card:
        return _update(card)
    p = _params(event)
    do = p.get("do", "")
    notice = ""
    if do in ADMIN_ONLY:
        if user.role != AIDLUser.Role.ADMIN:
            return _update(home_card(user, org), "Only your AIDL admins can do that.")
        return _admin_action(event, user, org, do, p)
    if do == "home":
        return _navigate({"popToRoot": True}, {"updateCard": home_card(user, org, event=event)})
    if do in ("approve", "start"):
        notice = ops.setup(user, org, {"action": do, "item": p.get("item", "")})
    elif do == "invite":
        notice = ops.invite(user, org, p.get("emails", ""))
    elif do == "remind":
        notice = ops.remind(user, org, p.get("who", ""))
    elif do == "agree_all":
        notice = ops.agree_all(user, org, p.get("cards", ""))
    elif do in ("start_learning", "agree", "traffic_light", "highway_code", "paw"):
        notice = ops.learner(user, org, {"action": "start" if do == "start_learning" else do,
                                         "n": p.get("n", ""), "version": p.get("version", ""),
                                         "id": p.get("id", "")})
    user.refresh_from_db()
    return _update(home_card(user, org, event=event), notice)
