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
from .slack_cards import AI_CATEGORIES, DATA_ALLOWED, IT_CATEGORIES, TRAFFIC_LIGHTS, build_admin_cards_context, coverage_csv, light_admin_context, coverage_data

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


def logo_view(org: Organization, meta: dict) -> dict:
    """Company logo for the licence and card images (header + faint watermark)."""
    blocks = [
        {"type": "section", "text": _md("Your logo appears on every AI Driving License and on the Highway Code "
                                        f"and Traffic Light cards for *{org.name}*: in the header and as a faint "
                                        "watermark, so they carry your company's mark and can't be passed off as "
                                        "anyone else's.")},
        _input("logo", "Logo (PNG or JPG, square works best)",
               {"type": "file_input", "action_id": "v", "filetypes": ["png", "jpg", "jpeg"], "max_files": 1},
               optional=bool(org.logo_data)),
    ]
    if org.logo_data:
        blocks.append(_input("remove", "Current logo", {"type": "checkboxes", "action_id": "v", "options": [
            _option("Remove the current logo", "remove")]}, optional=True,
            hint="A logo is set. Upload a new file to replace it, or tick to remove it."))
    return _modal("aidl_logo", "Company logo", blocks, submit="Save", meta=meta)


def submit_logo(org: Organization, view: dict) -> dict:
    values = _values(view)
    files = ((values.get("logo") or {}).get("v") or {}).get("files") or []
    remove = "remove" in (_value(values, "remove") or [])
    if not files and not remove:
        return errors(logo="Choose a file to upload.")
    meta = json.loads(view.get("private_metadata") or "{}")
    in_background(_save_logo, org.pk, files[0] if files else None, remove, meta.get("channel_id", ""))
    return {"notice": "✓ Logo removed." if remove and not files else "⏳ Saving your logo…"}


def _save_logo(org_pk, file_obj: dict | None, remove: bool, channel_id: str) -> None:
    """Download the uploaded file from Slack (files:read), store it as a PNG."""
    import requests

    from .licence_image import save_logo
    from .slack_blocks import publish_admin_center
    from .slack_onboarding import primary_admin

    org = Organization.objects.get(pk=org_pk)
    if file_obj is None:
        org.logo_data = ""
        org.save(update_fields=["logo_data", "updated_at"])
    else:
        token = slack_client.bot_token(org)
        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        if not url:
            info = slack_client.slack_api("files.info", token, params={"file": file_obj.get("id", "")})
            url = (info.get("file") or {}).get("url_private_download", "")
            if info.get("error") == "missing_scope":
                _tell_owner(org, "⚠️ AIDL couldn't read the logo file: the Slack app needs the *files:read* "
                                 "permission. Sign in with Slack again on the AIDL website (Organization → Slack → "
                                 "Allow) to grant it, then upload the logo again.")
                return
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            resp.raise_for_status()
            org.logo_data = save_logo(resp.content)
        except Exception:  # noqa: BLE001
            _tell_owner(org, "⚠️ AIDL couldn't read that logo file. Please upload a PNG or JPG image.")
            return
        org.save(update_fields=["logo_data", "updated_at"])
        _tell_owner(org, "✅ Your company logo is saved — it now appears on every AI Driving License and card for "
                         f"*{org.name}*.")
    admin = primary_admin(org)
    if admin is not None:
        publish_admin_center(org, admin)


def _tell_owner(org: Organization, text: str) -> None:
    from .slack_onboarding import primary_admin

    owner = primary_admin(org)
    if owner is not None and owner.microsoft_id.startswith("slack:"):
        slack_client.send_dm(slack_client.bot_token(org), org, owner.microsoft_id.split(":")[-1], text=text,
                             blocks=[{"type": "section", "text": _md(text)}])


SELECT_ALL = "__all__"
_MAX_LISTED = 99  # a Slack list holds 100 options; one is "Select all people"


def _person(uid: str, name: str) -> dict:
    return _option(name[:75], uid)


def _people_input(people: list[tuple[str, str]], selected: list[str], version: int) -> dict:
    """The People field: a searchable multi-select with "✅ Select all
    people" on top. Up to 99 people the list is in the popup; above that
    Slack asks AIDL for it as the admin types (people_options, needs the
    app's Options Load URL). Choosing "Select all people" redraws the popup
    with everyone selected (people_changed); a new block id each time makes
    Slack show the new selection."""
    names = dict(people)
    chosen = [_person(uid, names[uid]) for uid in selected if uid in names]
    if len(people) <= _MAX_LISTED:
        element = {"type": "multi_static_select", "action_id": "v", "placeholder": _text("Choose people"),
                   "options": [_option(f"✅ Select all people ({len(people)})", SELECT_ALL)]
                              + [_person(uid, name) for uid, name in people]}
    else:
        element = {"type": "multi_external_select", "action_id": "v", "placeholder": _text("Search people"),
                   "min_query_length": 0}
    if chosen:
        element["initial_options"] = chosen
    block = _input(f"people_{version}", "People", element, optional=True)
    block["dispatch_action"] = True  # tell AIDL when the selection changes (for "Select all people")
    return block


def add_people_view(meta: dict, d: dict, people: list[tuple[str, str]] | None = None,
                    selected: list[str] | None = None, version: int = 0) -> dict:
    """Setup step 3 — pick people; AIDL adds them to the AIDL channel and
    onboarding does the rest (no Add User form). `people` = the workspace
    members who can be added (not in the AIDL channel, no bots / guests)."""
    p = d.get("plan") or {}
    free = p.get("free")
    people = people or []
    if people:
        picker = [_people_input(people, selected or [], version)]
    else:
        picker = [{"type": "section", "text": _md("✅ Everyone in your workspace is already in the AIDL channel.")}]
    return _modal("aidl_add_people", "Add your team", [
        {"type": "section", "text": _md("Pick the people who should earn an AI licence. AIDL adds them to the AIDL "
                                        "channel and sends each one their welcome and learning cards.")},
        *picker,
        *([{"type": "context", "elements": [_md(f"*{free}* seat{'s' if free != 1 else ''} left on your "
                                                 f"{p.get('label', '')} plan ({p.get('used')} of {p.get('limit')} used)")]}]
          if free is not None else []),
    ], submit="Add people", meta={**meta, "people_version": version})


def people_options(org: Organization, query: str) -> list[dict]:
    """Options for the People search (lists over 99 people): "Select all
    people" first, then the matching people (Slack shows at most 100)."""
    people = workspace_members(org)
    q = (query or "").strip().lower()
    matches = [_person(uid, name) for uid, name in people if not q or q in name.lower()]
    return [_option(f"✅ Select all people ({len(people)})", SELECT_ALL)] + matches[:99]


def people_changed(org: Organization, admin: AIDLUser, view: dict, selected: list[str]) -> dict | None:
    """The People selection changed. "Select all people" → the same popup
    with every eligible person selected (deduplicated); otherwise nothing."""
    if SELECT_ALL not in selected:
        return None
    from .plans import usage

    meta = json.loads(view.get("private_metadata") or "{}")
    version = int(meta.get("people_version") or 0) + 1
    people = workspace_members(org)
    return add_people_view(meta, {"plan": usage(org)}, people, [uid for uid, _n in people], version)


def workspace_people(org: Organization) -> list[str]:
    """Slack ids of the workspace's real people (no bots, guests or
    deactivated accounts) who aren't in the AIDL channel yet."""
    return [uid for uid, _name in workspace_members(org)]


def workspace_members(org: Organization) -> list[tuple[str, str]]:
    """(Slack id, name) of the people workspace_people() returns, by name."""
    token = slack_client.bot_token(org)

    def pages(method: str, key: str, params: dict) -> list:
        items, cursor = [], ""
        while True:
            data = slack_client.slack_api(method, token, params={**params, "limit": 200, "cursor": cursor})
            items += data.get(key) or []
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor or not data.get("ok"):
                return items

    in_channel = set(pages("conversations.members", "members", {"channel": org.slack_channel_id}))
    people = [(u["id"], (u.get("profile") or {}).get("real_name") or u.get("real_name") or u.get("name") or u["id"])
              for u in pages("users.list", "members", {})
              if not (u.get("deleted") or u.get("is_bot") or u.get("is_app_user") or u.get("is_restricted")
                      or u.get("id") == "USLACKBOT" or u.get("id") in in_channel)]
    return sorted(people, key=lambda p: p[1].lower())


def submit_add_people(caller: AIDLUser, org: Organization, view: dict) -> dict:
    from .plans import usage
    from .slack_onboarding import onboard_member

    if not light_admin_context(caller)["can_team"]:
        return errors(people="Only the admin who set up AIDL can add people.")
    values = _values(view)
    block = next((b for b in values if b.startswith("people")), "people")
    field = values.get(block, {}).get("v", {}) or {}
    picked = field.get("selected_users") or [o["value"] for o in field.get("selected_options") or []]
    eligible = workspace_people(org)
    if SELECT_ALL in picked:  # "Select all people" chosen right before Add people
        picked = eligible
    else:  # still eligible (not added meanwhile), each person once
        allowed = set(eligible)
        picked = [uid for uid in dict.fromkeys(picked) if uid in allowed]
    if not picked:
        return errors(**{block: "Pick at least one person, or choose “Select all people”."})
    u = usage(org)
    if len(picked) > u["free"]:
        return errors(**{block: f"{len(picked)} selected, but only {u['free']} seat{'s' if u['free'] != 1 else ''} "
                                f"left on your {u['label']} plan ({u['used']} of {u['limit']} used). "
                                "Remove some people or upgrade."})
    token = slack_client.bot_token(org)

    def add_all():
        for uid in picked:
            slack_client.slack_api("conversations.invite", token, json={"channel": org.slack_channel_id, "users": uid})
            onboard_member(org, uid)  # also runs on the join event; the welcome is only sent once

    in_background(add_all)
    return {"notice": f"✓ Adding {len(picked)} {'person' if len(picked) == 1 else 'people'} to the AIDL channel — "
                      "they'll get their welcome and cards in a moment."}


def _perm_options(keys: list[str]) -> list[dict]:
    from .admin_perms import PERMS

    return [_option(f"{label} — {desc}", key) for key, _f, label, desc in PERMS if key in keys]


def add_admin_view(d: dict, meta: dict) -> dict:
    perms = _perm_options(d.get("grantable") or [])
    perm_input = ([_input("perms", "Permissions", {"type": "checkboxes", "action_id": "v", "options": perms,
                                                   "initial_options": [perms[0]]}, optional=True)] if perms else [])
    return _modal("aidl_add_admin", "Add another admin", [
        {"type": "section", "text": _md("Give a colleague access to your Admin Center — choose exactly what they can see and do.")},
        _input("person", "Pick them in Slack", {"type": "users_select", "action_id": "v", "placeholder": _text("Choose a person")}, optional=True),
        _input("email", "…or their email", {"type": "plain_text_input", "action_id": "v", "placeholder": _text("name@company.com")}, optional=True),
        *perm_input,
        {"type": "context", "elements": [_md("You can only give the permissions you have yourself.")]},
        {"type": "context", "elements": [_md(f"{d['admin_count']} of {d['admin_seat_limit']} admin seats used")]},
        *_admin_list_blocks(d),
    ], submit="Send Admin Invite", meta=meta)


def edit_admin_view(d: dict, target: dict, meta: dict) -> dict:
    """Change another admin's permissions (only the ones you have)."""
    perms = _perm_options(d.get("grantable") or [])
    chosen = [o for o in perms if o["value"] in target.get("keys", [])]
    element = {"type": "checkboxes", "action_id": "v", "options": perms}
    if chosen:
        element["initial_options"] = chosen
    blocks = [{"type": "section", "text": _md(f"*{target['name']}*  {target['email']}")}]
    if perms:
        blocks.append(_input("perms", "Permissions", element, optional=True))
    blocks.append({"type": "context", "elements": [_md("You can only change the permissions you have yourself. "
                                                       "The master admin is told about every change.")]})
    return _modal("aidl_admin_perms", "Edit permissions", blocks, submit="Save",
                  meta={**meta, "user_id": target["id"]})


def submit_edit_admin(caller: AIDLUser, org: Organization, view: dict) -> dict:
    from .admin_perms import FIELDS, apply, grantable, is_master, labels, notify_master

    meta = json.loads(view.get("private_metadata") or "{}")
    target = AIDLUser.objects.filter(pk=meta.get("user_id"), organization_id=str(org.pk),
                                     role=AIDLUser.Role.ADMIN).first()
    if target is None:
        return errors(perms="This person isn't an admin any more.")
    if is_master(org, target):
        return errors(perms="The master admin's permissions can't be changed.")
    apply(target, _value(_values(view), "perms") or [], grantable(org, caller))
    target.save(update_fields=[*FIELDS, "updated_at"])
    now = ", ".join(labels(target)) or "no permissions"
    in_background(notify_master, org, caller,
                  f"✏️ *{caller.full_name or caller.email}* changed *{target.full_name or target.email}*'s "
                  f"admin permissions: {now}.")
    return {"notice": f"✓ Saved — {target.full_name or target.email}: {now}."}


def _confirm(title: str, text: str, ok: str) -> dict:
    return {"title": _text(title), "text": _md(text), "confirm": _text(ok), "deny": _text("Cancel"), "style": "danger"}


def _admin_list_blocks(d: dict) -> list:
    """Current admins. The master admin can't be changed; admins with
    "Manage admins" can edit or remove the others."""
    from .admin_perms import MASTER_LABEL

    admins = d.get("admins") or []
    if not admins:
        return []
    blocks = [{"type": "divider"}, {"type": "section", "text": _md("*Current admins*")}]
    for a in admins:
        role = f"{MASTER_LABEL} · all permissions · can't be removed" if a["primary"] else (
            " · ".join(a["perms"]) or "No permissions")
        blocks.append({"type": "section", "text": _md(f"*{a['name']}*  {a['email']}\n_{role}_")})
        if d.get("can_admins") and not a["primary"]:
            blocks.append({"type": "actions", "block_id": f"aidl_admin_{a['id']}", "elements": [
                {"type": "button", "text": _text("✏️ Edit permissions"), "action_id": "aidl_admin_edit", "value": a["id"]},
                {"type": "button", "text": _text("Remove"), "style": "danger", "action_id": "aidl_admin_remove",
                 "value": a["id"],
                 "confirm": _confirm("Remove admin?", f"*{a['name']}* will lose Admin Center access. If they're in the "
                                     "AIDL channel they become a team member (or are removed if your plan is full).",
                                     "Remove admin")},
            ]})
    if d.get("notice"):
        blocks.append({"type": "context", "elements": [_md(d["notice"])]})
    return blocks


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


def _bar(done: int, total: int) -> str:
    pct = round(done / total * 100) if total else 0
    filled = round(pct / 10)
    return f"`{'▰' * filled}{'▱' * (10 - filled)}`  *{done}/{total}* · {pct}%"


def team_progress_view(org: Organization, notice: str = "") -> dict:
    """Admin progress view: every team member's journey to the Learner
    licence, grouped by where they are (admins aren't part of the team)."""
    from .licensing import team_progress
    from .plans import usage

    t = team_progress(org)
    u = usage(org)
    n = t["members"]
    step = lambda when: f"✅ {when.strftime('%d %b')}" if when else "⬜ not yet"  # noqa: E731
    blocks = [
        *([{"type": "section", "text": _md(notice)}] if notice else []),
        {"type": "context", "elements": [_md(f"*{org.name}*  ·  {n} team member{'s' if n != 1 else ''}  ·  admins not counted")]},
        {"type": "context", "elements": [_md(
            f"*{u['label']} plan*  ·  {u['used']} of {u['limit']} users  ·  {u['package_label']} ({u['cards']} cards)")]},
        {"type": "section", "text": _md(
            f"🪪  *Licensed*\n{_bar(t['licensed'], n)}\n\n"
            f"📖  *Highway Code accepted*\n{_bar(t['highway_code'], n)}\n\n"
            f"🚦  *Traffic Light accepted*\n{_bar(t['traffic_light'], n)}\n\n"
            f"📄  *AUP — all 14 points accepted*{'' if t['aup_version'] else ' (not created)'}\n{_bar(t['aup'], n)}")},
    ]
    if t["legacy"]:
        blocks.append({"type": "context", "elements": [_md(
            f"{t['legacy']} license(s) came from the old *Add User* flow, before the acknowledgement rule.")]})

    groups = (
        ("licensed", "🪪  LICENSED"), ("legacy", "🪪  LICENSED (BEFORE ACKNOWLEDGEMENT RULE)"),
        ("waiting_seat", "⏳  WAITING FOR A SEAT"), ("in_progress", "🟡  IN PROGRESS"),
        ("not_started", "⬜  NOT STARTED"),
    )
    shown = 0
    for status, title in groups:
        rows = [r for r in t["rows"] if r["status"] == status]
        if not rows:
            continue
        blocks += [{"type": "divider"}, {"type": "section", "text": _md(f"*{title}*  ({len(rows)})")}]
        for r in rows:
            if shown >= 35:  # Slack allows 100 blocks per view; the CSV has everyone
                break
            m = r["member"]
            who = f"*{r['name']}*\n{m.email or r['role'].title()}"
            if m.licence_issued and m.licence_number:
                who += f"\n`{m.licence_number}`"
            blocks.append({"type": "section", "fields": [
                _md(who),
                _md(f"📄 AUP  {'✅' if r['aup_at'] else '⬜'} {r['aup_points']}/14 points\n📖 Highway Code  {step(r['highway_code_at'])}\n"
                    f"🚦 Traffic Light  {step(r['traffic_light_at'])}"),
            ], "accessory": {
                "type": "button", "text": _text("Remove"), "action_id": "aidl_member_remove", "value": str(m.pk),
                "confirm": _confirm("Remove from AIDL?", f"*{r['name']}* will be taken out of the AIDL channel and "
                                    "their cards stop." + (" Their license is suspended and still counts toward "
                                    "your plan." if m.licence_issued else " Their seat becomes free."), "Remove"),
            }})
            shown += 1
    if t["removed"]:
        lines = [f"• *{r['name']}*  ·  removed {r['left_at'].strftime('%d %b')}"
                 + ("  ·  license suspended, seat still counted" if r["licensed"] else "  ·  seat freed")
                 for r in t["removed"][:15]]
        blocks += [{"type": "divider"}, {"type": "section", "text": _md(
            f"*🚪  REMOVED FROM CHANNEL*  ({len(t['removed'])})\n" + "\n".join(lines))}]
    if len(t["rows"]) > shown:
        blocks.append({"type": "context", "elements": [_md(f"…and {len(t['rows']) - shown} more in the Coverage CSV.")]})
    if not t["rows"]:
        blocks += [{"type": "divider"}, {"type": "section", "text": _md(
            "No one has joined yet — add people to the AIDL channel and AIDL onboards them automatically.")}]
    return _modal("aidl_team_progress", "Team progress", blocks, close="Close")


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


def in_background(fn, *args, **kwargs) -> None:
    """Slack gives a modal submission 3 seconds; slower follow-ups (channel
    invite, DMs) run after the response."""
    from django.conf import settings

    if settings.SLACK_REPLY_SYNC:
        fn(*args, **kwargs)
    else:
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


# ---------- submissions ----------

def submit_add_admin(caller: AIDLUser, org: Organization, team_id: str, view: dict) -> dict | None:
    values = _values(view)
    d = light_admin_context(caller)
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
    from .admin_perms import FIELDS, PERMS, apply, grantable, labels, notify_master

    perms = _value(values, "perms") or []
    admin.role = AIDLUser.Role.ADMIN
    apply(admin, [], [k for k, *_ in PERMS])        # a new admin starts with nothing…
    apply(admin, perms, grantable(org, caller))   # …and gets what the caller may give
    admin.save(update_fields=["role", *FIELDS, "updated_at"])
    in_background(_onboard_admin, token, org, caller, admin, slack_user_id)
    in_background(notify_master, org, caller,
                  f"👥 *{caller.full_name or caller.email}* added *{admin.full_name or admin.email}* as an admin "
                  f"({', '.join(labels(admin)) or 'no permissions'}).")
    return {"notice": f"✓ {d['admin_count'] + 1} of {d['admin_seat_limit']} admin seats used · invited {admin.email or admin.full_name}"}


def _onboard_admin(token: str, org: Organization, caller: AIDLUser, admin: AIDLUser, slack_user_id: str) -> None:
    """Guide 8.2: add the new admin to the channel and send them the Admin
    cards, showing only the tabs their permissions allow."""
    from .slack_blocks import admin_card_blocks

    _welcome_to_channel(token, org, slack_user_id)
    result = slack_client.send_dm(
        token, org, slack_user_id,
        text=f"{caller.full_name} has added you as an AIDL admin for {org.name}",
        blocks=[{"type": "section", "text": _md(f"*{caller.full_name}* has added you as an AIDL admin for *{org.name}*.")}]
        + admin_card_blocks(build_admin_cards_context(admin), "home"),
    )
    _warn_if_dm_failed(token, org, caller, admin, [result])


def _warn_if_dm_failed(token: str, org: Organization, caller: AIDLUser, person: AIDLUser, results: list) -> None:
    """Tell the admin privately when the new person's DMs couldn't be sent."""
    failed = next((r for r in results if not r.get("ok")), None)
    if failed is None or not org.slack_channel_id:
        return
    reason = slack_client.DM_BLOCKED.get(failed.get("error"), f"Slack said: {failed.get('error')}")
    where = f" They were shown the cards privately in <#{org.slack_channel_id}> instead." if failed.get("fallback_ok") else ""
    caller_id = caller.microsoft_id.split(":")[-1]
    slack_client.slack_api("chat.postEphemeral", token, json={
        "channel": org.slack_channel_id, "user": caller_id,
        "text": f"⚠️ AIDL couldn't send {person.full_name or person.email} a direct message: {reason}.{where}"})


def _card(d: dict, card_id: str) -> dict | None:
    return next((c for c in d["cards"] if c["id"] == card_id), None)


def submit_card_request(caller: AIDLUser, org: Organization, view: dict) -> dict:
    meta = json.loads(view.get("private_metadata") or "{}")
    try:
        request_card(org, card_id=meta.get("card_id", ""), by_email=caller.email)
    except CardsError as exc:
        return {"response_action": "update", "view": _modal("aidl_card", "Send Cards", [
            {"type": "section", "text": _md(exc.message or "Monthly quota reached.")}], close="Close")}
    card = _card(light_admin_context(caller, cards=True), meta["card_id"])
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
        from .models import TrafficLightRating
        from .slack_blocks import rating_blocks

        rating = TrafficLightRating.objects.first()
        blocks += rating_blocks(rating.likes if rating else 128, rating.dislikes if rating else 6)
    else:
        blocks += [{"type": "section", "text": _md(p)} for p in card["body"]]
    return blocks


def submit_card_send(caller: AIDLUser, org: Organization, view: dict) -> dict | None:
    meta = json.loads(view.get("private_metadata") or "{}")
    values = _values(view)
    d = light_admin_context(caller, cards=True)
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
        if card.get("lights"):
            row.lights = ",".join(lights)
        row.save()
    dots = "".join(_LIGHT_EMOJI[l] for l in lights) if card.get("lights") else ""
    status = "🕒 Scheduled" if later else "✓ Sent"
    return {"notice": f"{status} · {card['title']} {dots}".strip()}


def submit_new_card(caller: AIDLUser, org: Organization, view: dict) -> dict | None:
    values = _values(view)
    name = _value(values, "name")
    if not name:
        return errors(name="Give the new card a name.")
    d = light_admin_context(caller, cards=True)
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
