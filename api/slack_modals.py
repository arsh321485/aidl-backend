"""Slack modals (pop-ups) for the Admin card actions — AIDL Slack guide 8.2–8.6
and 9. Opened from the card buttons in slack_interactions.py; each
submission is validated here and saved to the database.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from . import slack_client
from .cards_service import CardsError, request_card, request_new_card
from .admin_ops import AdminOpsError, add_registered_app
from .models import AIDLUser, CardRequest, Organization, RegisteredApp
from .org_policy import get_answers, policy_effects
from .slack_cards import AI_CATEGORIES, DATA_ALLOWED, IT_CATEGORIES, TRAFFIC_LIGHTS, build_admin_cards_context, coverage_csv, coverage_data

_LIGHT_EMOJI = {"green": "🟢", "amber": "🟡", "red": "🔴"}
_DATA_VALUE = {"Public only": "public_only", "Internal": "internal", "Internal + Confidential": "internal_confidential", "None": "none"}


# ---------- Block Kit helpers ----------

def _text(text: str) -> dict:
    return {"type": "plain_text", "text": text, "emoji": True}


def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _option(label: str, value: str | None = None) -> dict:
    return {"text": _text(label), "value": value or label}


def _input(block_id: str, label: str, element: dict, *, optional: bool = False, hint: str = "") -> dict:
    block = {"type": "input", "block_id": block_id, "label": _text(label), "element": element, "optional": optional}
    if hint:
        block["hint"] = _text(hint)
    return block


def _modal(callback_id: str, title: str, blocks: list, *, submit: str = "", meta: dict | None = None, close: str = "Cancel") -> dict:
    view = {
        "type": "modal",
        "callback_id": callback_id,
        "title": _text(title[:24]),
        "close": _text(close),
        "blocks": blocks,
        "private_metadata": json.dumps(meta or {}),
    }
    if submit:
        view["submit"] = _text(submit)
    return view


def _values(view: dict) -> dict:
    return view.get("state", {}).get("values", {})


def _value(values: dict, block_id: str, action_id: str = "v"):
    el = values.get(block_id, {}).get(action_id, {})
    if "value" in el:
        return (el.get("value") or "").strip()
    if "selected_option" in el:
        return (el.get("selected_option") or {}).get("value", "")
    if "selected_user" in el:
        return el.get("selected_user") or ""
    if "selected_options" in el:
        return [o["value"] for o in el.get("selected_options") or []]
    if "selected_date_time" in el:
        return el.get("selected_date_time")
    return ""


def errors(**fields) -> dict:
    return {"response_action": "errors", "errors": fields}


# ---------- views ----------

def add_admin_view(d: dict, meta: dict) -> dict:
    perms = [
        _option("Approve Apps — change the AI / IT registry", "approve_apps"),
        _option("Access Cards — view and send reference cards", "access_cards"),
        _option("Create Card — request new cards", "create_card"),
    ]
    return _modal("aidl_add_admin", "Add another admin", [
        {"type": "section", "text": _md("Give a colleague access to your Admin Center — choose exactly what they can see and do.")},
        _input("person", "Pick them in Slack", {"type": "users_select", "action_id": "v", "placeholder": _text("Choose a person")}, optional=True),
        _input("email", "…or their email", {"type": "plain_text_input", "action_id": "v", "placeholder": _text("name@company.com")}, optional=True),
        _input("perms", "Permissions", {"type": "checkboxes", "action_id": "v", "options": perms, "initial_options": [perms[0]]}, optional=True),
        {"type": "context", "elements": [_md(f"{d['admin_count']} of {d['admin_seat_limit']} admin seats used")]},
    ], submit="Send Admin Invite", meta=meta)


def add_user_view(d: dict, meta: dict) -> dict:
    return _modal("aidl_add_user", "Add a team member", [
        {"type": "section", "text": _md("Issue a license to a team member — they get the reference cards and can request more for their channel. No Admin Center access.")},
        _input("name", "Name", {"type": "plain_text_input", "action_id": "v", "placeholder": _text("e.g. Arjun Mehta")}),
        _input("person", "Pick them in Slack", {"type": "users_select", "action_id": "v", "placeholder": _text("Choose a person")}, optional=True),
        _input("email", "…or their email", {"type": "plain_text_input", "action_id": "v", "placeholder": _text("arjun@company.com")}, optional=True),
        {"type": "context", "elements": [_md(f"{d['licences_issued']} of {d['seats_purchased']} licenses issued")]},
    ], submit="Issue License", meta=meta)


def cards_view(d: dict, meta: dict) -> dict:
    blocks = [
        {"type": "section", "text": _md("Pick the reference cards your team needs at the wheel — view one, request it, then send it into the channel.")},
        {"type": "section", "text": _md(f"*MONTHLY CARD QUOTA*\n`{d['quota_used']} of {d['quota_max']}` cards requested this month\n📅 All cards available {d['available_from']} – {d['available_to']}")},
        {"type": "divider"},
    ]
    if d["can_manage_cards"]:
        for c in d["cards"]:
            state = {"sent": " · ✓ Sent", "scheduled": " · 🕒 Scheduled", "requested": " · ✓ Requested",
                     "unavailable": " · Not available yet"}.get(c["state"], f" · {c['price']} · one-time")
            badge = " ⭐ _Recommended_" if c.get("recommended") else ""
            blocks.append({
                "type": "section",
                "text": _md(f"{c['icon']} *{c['title']}*{badge}\n_{c['kicker']}_{state}\n{c['desc']}"),
                "accessory": {"type": "button", "text": _text("View"), "action_id": "aidl_card_view", "value": c["id"]},
            })
    if d["can_create_card"]:
        blocks += [
            {"type": "divider"},
            {"type": "section", "text": _md("*Need something that's not listed?* We'll scope it and follow up with pricing."),
             "accessory": {"type": "button", "text": _text("＋ Request a New Card"), "action_id": "aidl_card_new", "value": "new"}},
        ]
    return _modal("aidl_cards", "Send Cards", blocks, meta=meta, close="Close")


def card_view(d: dict, card: dict, meta: dict) -> dict:
    meta = dict(meta, card_id=card["id"])
    blocks = [{"type": "context", "elements": [_md(f"*{card['kicker'].upper()}*")]}]
    blocks += [{"type": "section", "text": _md(p)} for p in card["body"]]
    state = card["state"]
    if state == "unavailable":
        blocks.append({"type": "context", "elements": [_md("Not available yet — publish your AI policy first.")]})
        return _modal("aidl_card", card["title"], blocks, meta=meta, close="Close")
    if state == "not_requested":
        if d["quota_used"] >= d["quota_max"]:
            blocks.append({"type": "context", "elements": [_md("Monthly quota reached — requests open again next month.")]})
            return _modal("aidl_card", card["title"], blocks, meta=meta, close="Close")
        blocks.append({"type": "context", "elements": [_md(f"*{card['price']} · one-time* · uses 1 of your monthly quota")]})
        return _modal("aidl_card_request", card["title"], blocks, submit="Request Card", meta=meta)
    if state == "sent":
        blocks.append({"type": "context", "elements": [_md("✓ Sent to the channel")]})
        return _modal("aidl_card", card["title"], blocks, meta=meta, close="Close")
    return card_send_view(card, meta, blocks)


def card_send_view(card: dict, meta: dict, blocks: list | None = None) -> dict:
    blocks = list(blocks or [])
    status = "🕒 Scheduled — " + card["scheduled_for"] if card["state"] == "scheduled" else "✓ Requested"
    blocks.append({"type": "context", "elements": [_md(status)]})
    if card.get("lights"):
        lights = [_option(f"{_LIGHT_EMOJI[l['id']]} {l['label']} — {l['tagline']}", l["id"]) for l in TRAFFIC_LIGHTS]
        blocks.append(_input("lights", "Choose which lights to send", {
            "type": "checkboxes", "action_id": "v", "options": lights, "initial_options": lights,
        }, optional=True))
    when = [_option("Send now", "now"), _option("Schedule for later", "later")]
    blocks.append(_input("when", "When", {"type": "radio_buttons", "action_id": "v", "options": when, "initial_option": when[0]}))
    blocks.append(_input("at", "Date and time (for Schedule for later)", {"type": "datetimepicker", "action_id": "v"}, optional=True))
    return _modal("aidl_card_send", card["title"], blocks, submit="Send Card", meta=meta)


def new_card_view(meta: dict) -> dict:
    return _modal("aidl_card_new", "Request a New Card", [
        _input("name", "Card name", {"type": "plain_text_input", "action_id": "v", "placeholder": _text("e.g. Vendor Risk Checklist")}),
        _input("desc", "What should it cover?", {"type": "plain_text_input", "action_id": "v", "multiline": True}, optional=True),
        _input("priority", "Priority", {"type": "static_select", "action_id": "v", "options": [_option("Standard"), _option("Urgent")],
                                        "initial_option": _option("Standard")}),
        {"type": "context", "elements": [_md("Counts toward your monthly quota · pricing scoped after review")]},
    ], submit="Submit Request", meta=meta)


def add_app_view(kind: str, meta: dict) -> dict:
    cats = AI_CATEGORIES if kind == "ai" else IT_CATEGORIES
    data = [_option(f"{ {'g': '🟢', 'a': '🟡', 'r': '🔴'}[dot] } {label}", label) for label, dot in DATA_ALLOWED]
    status = [_option("Approved"), _option("Prohibited")]
    title = "Add AI Application" if kind == "ai" else "Add IT Application"
    return _modal("aidl_add_app", title, [
        _input("name", "Application name", {"type": "plain_text_input", "action_id": "v", "placeholder": _text("e.g. Claude Enterprise")}),
        _input("category", "Category", {"type": "static_select", "action_id": "v", "options": [_option(c) for c in cats]}),
        _input("data", "Data allowed", {"type": "static_select", "action_id": "v", "options": data, "initial_option": data[0]}),
        _input("status", "Status", {"type": "static_select", "action_id": "v", "options": status, "initial_option": status[0]},
               hint="Status drives whether your team can use this application"),
    ], submit="＋ Add", meta=dict(meta, kind=kind))


def coverage_view(org: Organization, download_url: str = "", note: str = "") -> dict:
    """Guide 8.1 — the coverage report as a Slack popup (no web page). The
    CSV file itself arrives as a DM from AIDL (send_coverage_csv)."""
    data = coverage_data(org)
    rows = data["rows"]
    blocks = [
        {"type": "section", "text": _md("Every team member with their license class, AUP signature and the reference cards they've received.")},
        {"type": "section", "fields": [
            _md(f"*MEMBERS*\n{len(rows)}"),
            _md(f"*LICENSED*\n{data['licensed']}"),
            _md(f"*AUP UNSIGNED*\n{data['unsigned']}"),
            _md(f"*CARDS SENT*\n{data['cards_sent']}"),
        ]},
        _file_block(download_url, note),
        {"type": "divider"},
    ]
    shown = rows[:40]  # Slack allows 100 blocks per view; the CSV has everyone.
    for r in shown:
        licence = f"L · `{r['license_number']}`" if r["license_class"] else "no license yet"
        aup = "✅ AUP signed" if r["aup_signed"] == "yes" else "❌ AUP unsigned"
        blocks.append({"type": "section", "text": _md(
            f"*{r['name'] or r['email']}* · {r['role'].title()}\n{r['email']}\n{licence} · {aup} · {r['cards_received']} cards")})
    if len(rows) > len(shown):
        blocks.append({"type": "context", "elements": [_md(f"…and {len(rows) - len(shown)} more in the CSV file.")]})
    if not rows:
        blocks.append({"type": "context", "elements": [_md("No team members yet.")]})
    return _modal("aidl_coverage", "Coverage report", blocks, close="Close")


def _file_block(download_url: str, note: str) -> dict:
    """Download CSV button: a private, 60-minute AIDL link (only this admin
    sees the modal) that saves aidl-coverage.csv straight away."""
    text = note or "📎 *aidl-coverage.csv* — a copy is also sent to your direct messages with AIDL."
    block = {"type": "section", "text": _md(text)}
    if download_url:
        block["accessory"] = {"type": "button", "text": _text("⬇ Download CSV"), "action_id": "aidl_link_csv",
                              "url": download_url, "style": "primary"}
    return block


def send_coverage_csv(org: Organization, slack_user_id: str) -> dict:
    data = coverage_data(org)
    return slack_client.send_file_to_user(
        slack_client.bot_token(org),
        slack_user_id,
        filename="aidl-coverage.csv",
        content=coverage_csv(data["rows"]),
        title=f"AIDL coverage — {org.name}",
        comment=f"⬇ Coverage report for *{org.name}* · {len(data['rows'])} members",
    )


def view_for_tab(tab: str, d: dict, meta: dict) -> dict | None:
    return {
        "add-admin": lambda: add_admin_view(d, meta),
        "add-user": lambda: add_user_view(d, meta),
        "cards": lambda: cards_view(d, meta),
        "ai-apps": lambda: add_app_view("ai", meta),
        "it-apps": lambda: add_app_view("it", meta),
    }.get(tab, lambda: None)()


# ---------- people ----------

def _resolve_slack_user(token: str, values: dict) -> tuple[str, dict]:
    """Returns (slack_user_id, profile) from the user picker or the email."""
    user_id = _value(values, "person")
    email = _value(values, "email")
    if user_id:
        data = slack_client.slack_api("users.info", token, params={"user": user_id})
    elif email:
        data = slack_client.slack_api("users.lookupByEmail", token, params={"email": email})
    else:
        return "", {}
    if not data.get("ok"):
        return "", {}
    user = data.get("user") or {}
    if user.get("is_bot") or user.get("deleted"):
        return "", {}
    return user.get("id", ""), user.get("profile") or {}


def _upsert_member(org: Organization, team_id: str, slack_user_id: str, profile: dict, *, name: str = "") -> AIDLUser | None:
    """The AIDL account for a Slack member of this organization. Returns None
    when that email already belongs to another organization."""
    microsoft_id = f"slack:{team_id}:{slack_user_id}"
    email = (profile.get("email") or "").strip()
    user = AIDLUser.objects.filter(microsoft_id=microsoft_id).first()
    if user is None and email:
        user = AIDLUser.objects.filter(email__iexact=email).first()
    if user is not None and user.organization_id and user.organization_id != str(org.pk):
        return None
    if user is None:
        user = AIDLUser(
            microsoft_id=microsoft_id, provider="slack", email=email, is_active=True,
            enroll_as=AIDLUser.EnrollAs.ORGANIZATION, role=AIDLUser.Role.LEARNER,
        )
    user.full_name = name or user.full_name or profile.get("real_name") or profile.get("display_name") or email
    user.avatar_url = user.avatar_url or profile.get("image_192") or ""
    user.organization_id = str(org.pk)
    user.organization_name = org.name
    user.save()
    return user


def _welcome_to_channel(token: str, org: Organization, slack_user_id: str) -> None:
    if org.slack_channel_id:
        slack_client.slack_api("conversations.invite", token, json={"channel": org.slack_channel_id, "users": slack_user_id})


def in_background(fn, *args) -> None:
    """Slack gives a modal submission 3 seconds; slower follow-ups (channel
    invite, DMs) run after the response."""
    from django.conf import settings

    if settings.SLACK_REPLY_SYNC:
        fn(*args)
    else:
        threading.Thread(target=fn, args=args, daemon=True).start()


# ---------- submissions ----------

def submit_add_admin(caller: AIDLUser, org: Organization, team_id: str, view: dict) -> dict | None:
    values = _values(view)
    d = build_admin_cards_context(caller)
    if d["admin_count"] >= d["admin_seat_limit"]:
        return errors(email=f"{d['admin_seat_limit']} of {d['admin_seat_limit']} admin seats used · no seats left")
    if not _value(values, "person") and not _value(values, "email"):
        return errors(email="Pick a person or enter their email.")
    token = slack_client.bot_token(org)
    slack_user_id, profile = _resolve_slack_user(token, values)
    if not slack_user_id:
        return errors(email="This person isn't in your Slack workspace yet — invite them to the workspace first.")
    admin = _upsert_member(org, team_id, slack_user_id, profile)
    if admin is None:
        return errors(email="This person already belongs to another AIDL organization.")
    perms = _value(values, "perms") or []
    admin.role = AIDLUser.Role.ADMIN
    admin.perm_approve_apps = "approve_apps" in perms
    admin.perm_access_cards = "access_cards" in perms
    admin.perm_create_card = "create_card" in perms
    admin.save(update_fields=["role", "perm_approve_apps", "perm_access_cards", "perm_create_card", "updated_at"])
    in_background(_onboard_admin, token, org, caller, admin, slack_user_id)
    return {"notice": f"✓ {d['admin_count'] + 1} of {d['admin_seat_limit']} admin seats used · invited {admin.email or admin.full_name}"}


def submit_add_user(caller: AIDLUser, org: Organization, team_id: str, view: dict) -> dict | None:
    values = _values(view)
    name = _value(values, "name")
    d = build_admin_cards_context(caller)
    if not name:
        return errors(name="Enter their name.")
    if d["licences_issued"] >= d["seats_purchased"]:
        return errors(name=f"{d['seats_purchased']} of {d['seats_purchased']} licenses issued · no seats left")
    if not _value(values, "person") and not _value(values, "email"):
        return errors(email="Pick a person or enter their email.")
    token = slack_client.bot_token(org)
    slack_user_id, profile = _resolve_slack_user(token, values)
    if not slack_user_id:
        return errors(email="This person isn't in your Slack workspace yet — invite them to the workspace first.")
    member = _upsert_member(org, team_id, slack_user_id, profile, name=name)
    if member is None:
        return errors(email="This person already belongs to another AIDL organization.")
    if not member.licence_issued:
        now = timezone.now()
        member.licence_issued = True
        member.licence_number = member.licence_number or f"AIDL-L-{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
        member.licence_issued_at = now
        member.licence_expires_at = now + timedelta(days=365)
        member.save(update_fields=["licence_issued", "licence_number", "licence_issued_at", "licence_expires_at", "updated_at"])
    in_background(_onboard_user, token, org, member, slack_user_id)
    return {"notice": f"✓ {d['licences_issued'] + 1} of {d['seats_purchased']} licenses issued · invited {member.email or member.full_name}"}


def _onboard_admin(token: str, org: Organization, caller: AIDLUser, admin: AIDLUser, slack_user_id: str) -> None:
    """Guide 8.2: add the new admin to the channel and send them the Admin
    cards, showing only the tabs their permissions allow."""
    from .slack_blocks import admin_card_blocks

    _welcome_to_channel(token, org, slack_user_id)
    slack_client.slack_api("chat.postMessage", token, json={
        "channel": slack_user_id,
        "text": f"{caller.full_name} has added you as an AIDL admin for {org.name}",
        "blocks": [{"type": "section", "text": _md(f"*{caller.full_name}* has added you as an AIDL admin for *{org.name}*.")}]
        + admin_card_blocks(build_admin_cards_context(admin), "home"),
    })


def _onboard_user(token: str, org: Organization, member: AIDLUser, slack_user_id: str) -> None:
    """Guide 9 / 10.1–10.2: add the user to the channel, then DM the Welcome
    message followed by the License card."""
    from .slack_blocks import user_license_blocks, user_welcome_blocks

    _welcome_to_channel(token, org, slack_user_id)
    first = (member.full_name or "there").split()[0]
    slack_client.slack_api("chat.postMessage", token, json={
        "channel": slack_user_id, "text": f"Welcome to AIDL, {first}!", "blocks": user_welcome_blocks(member, org)})
    slack_client.slack_api("chat.postMessage", token, json={
        "channel": slack_user_id, "text": "Your Learner's Permit is ready", "blocks": user_license_blocks(member, org)})


def _card(d: dict, card_id: str) -> dict | None:
    return next((c for c in d["cards"] if c["id"] == card_id), None)


def submit_card_request(caller: AIDLUser, org: Organization, view: dict) -> dict:
    meta = json.loads(view.get("private_metadata") or "{}")
    try:
        request_card(org, card_id=meta.get("card_id", ""), by_email=caller.email)
    except CardsError as exc:
        return {"response_action": "update", "view": _modal("aidl_card", "Send Cards", [
            {"type": "section", "text": _md(exc.message or "Monthly quota reached.")}], close="Close")}
    card = _card(build_admin_cards_context(caller), meta["card_id"])
    return {"response_action": "update", "view": card_send_view(card, meta)}


def _reference_card_blocks(card: dict, org: Organization, lights: list[str]) -> list:
    blocks = [
        {"type": "context", "elements": [_md(f"*AIDL* · APP  for {org.name}")]},
        {"type": "header", "text": _text(f"{card['icon']} {card['title']}")},
        {"type": "context", "elements": [_md(f"*{card['kicker'].upper()}*")]},
    ]
    if card.get("lights"):
        fx = policy_effects(get_answers(org))
        blocks.append({"type": "section", "text": _md("Before you paste anything into an AI, check the lights.")})
        for light in TRAFFIC_LIGHTS:
            if light["id"] not in lights:
                continue
            items = list(light["items"])
            if light["id"] == "red" and fx["red_includes_confidential"]:
                items.append("All confidential and customer data")
            blocks.append({"type": "section", "text": _md(
                f"{_LIGHT_EMOJI[light['id']]} *{light['label']}* — _{light['tagline']}_\n"
                + "\n".join(f"• {i}" for i in items) + f"\n*→ {light['action']}*")})
    else:
        blocks += [{"type": "section", "text": _md(p)} for p in card["body"]]
    return blocks


def submit_card_send(caller: AIDLUser, org: Organization, view: dict) -> dict | None:
    meta = json.loads(view.get("private_metadata") or "{}")
    values = _values(view)
    d = build_admin_cards_context(caller)
    card = _card(d, meta.get("card_id", ""))
    if card is None:
        return errors(when="Unknown card.")
    lights = ["green", "amber", "red"]
    if card.get("lights"):
        lights = _value(values, "lights") or []
        if not lights:
            return errors(lights="Select at least one light to send.")
    later = _value(values, "when") == "later"
    post_at = _value(values, "at") if later else None
    if later and not post_at:
        return errors(at="Pick the date and time to send it.")
    if later and post_at <= timezone.now().timestamp() + 60:
        return errors(at="Pick a time in the future.")

    token = slack_client.bot_token(org)
    text = f"{card['icon']} {card['title']}"
    blocks = _reference_card_blocks(card, org, lights)
    if later:
        result = slack_client.slack_api("chat.scheduleMessage", token, json={
            "channel": org.slack_channel_id, "post_at": int(post_at), "text": text, "blocks": blocks})
    else:
        result = slack_client.slack_api("chat.postMessage", token, json={
            "channel": org.slack_channel_id, "text": text, "blocks": blocks})
    if not result.get("ok"):
        return errors(when=f"Slack couldn't post the card ({result.get('error')}).")

    row = CardRequest.objects.filter(organization_id=str(org.pk), card_id=card["id"]).order_by("-requested_at").first()
    if row is not None:
        if later:
            row.status = CardRequest.Status.SCHEDULED
            row.scheduled_at = datetime.fromtimestamp(int(post_at), tz=dt_timezone.utc)
        else:
            row.status = CardRequest.Status.SENT
            row.sent_at = timezone.now()
        row.save()
    dots = "".join(_LIGHT_EMOJI[l] for l in lights) if card.get("lights") else ""
    status = "🕒 Scheduled" if later else "✓ Sent"
    return {"notice": f"{status} · {card['title']} {dots}".strip()}


def submit_new_card(caller: AIDLUser, org: Organization, view: dict) -> dict | None:
    values = _values(view)
    name = _value(values, "name")
    if not name:
        return errors(name="Give the new card a name.")
    d = build_admin_cards_context(caller)
    if d["quota_used"] >= d["quota_max"]:
        return errors(name="Monthly quota reached — new requests open next month.")
    desc = _value(values, "desc")
    priority = _value(values, "priority") or "Standard"
    request_new_card(org, by_email=caller.email, title=name, description=f"[{priority}] {desc}".strip())
    # A custom request uses one of the monthly quota (guide 8.4).
    CardRequest.objects.create(organization_id=str(org.pk), card_id=f"custom:{name[:40]}",
                               status=CardRequest.Status.REQUESTED, requested_by_email=caller.email)
    return {"notice": f"⏳ Pending Review · {name} ({priority})"}


def submit_add_app(caller: AIDLUser, org: Organization, view: dict) -> dict | None:
    meta = json.loads(view.get("private_metadata") or "{}")
    values = _values(view)
    name = _value(values, "name")
    if not name:
        return errors(name="Enter the application name.")
    kind = meta.get("kind", "ai")
    status = RegisteredApp.Status.APPROVED if _value(values, "status") == "Approved" else RegisteredApp.Status.REJECTED
    try:
        add_registered_app(
            org,
            app_type=RegisteredApp.AppType.AI if kind == "ai" else RegisteredApp.AppType.IT,
            name=name,
            category=_value(values, "category") or "Other",
            data_allowed=_DATA_VALUE.get(_value(values, "data"), ""),
            status=status,
        )
    except AdminOpsError as exc:
        return errors(name=exc.message or exc.code)
    label = "Approved" if status == RegisteredApp.Status.APPROVED else "Prohibited"
    return {"notice": f"✓ Added {name} to {'AI' if kind == 'ai' else 'IT'} Applications · {label}"}
