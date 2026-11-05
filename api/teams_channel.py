"""AIDL in Microsoft Teams without a bot: the same programme as Slack, in a channel.

Graph lets AIDL post cards in the channel as a signed-in admin (their saved
Microsoft login, get_access_token_for_user), but a card without a bot can't
act when a button is pressed. So every button opens AIDL's own pages
(teams_portal.py), where people sign in with Microsoft and press I agree /
Got it. What each person did is saved per person, as in Slack.

    Slack                         Teams
    DM to each person             one post in the channel, @mentioning people
    private DM to an admin        email
    chat.scheduleMessage          PackageDelivery rows + dispatch(), run hourly
    channel join / leave events   sync_team(), compares the Team's members

The rules (cards, approvals, licences, plans) are the same modules Slack uses.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta
from html import escape

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError
from django.utils import timezone

from .graph_client import GRAPH_BASE, graph_headers
from .models import AIDLUser, Organization, PackageDelivery, TeamsPost

logger = logging.getLogger(__name__)
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"  # consumer Microsoft accounts: no Teams organization
_TOKENS: dict = {}
_TOKEN_SECONDS = 30 * 60
MAX_MENTIONS = 20
SEND_HOUR = 10
DISPATCH_EVERY = timedelta(minutes=15)
HOME_SETTLE_SECONDS = (3, 5, None)  # the wait after each try; None = the last one


def is_teams(org: Organization | None) -> bool:
    return bool(org is not None and (org.teams_team_id or org.ms_tenant_id) and not org.slack_team_id)


def app_url(path: str = "") -> str:
    return f"{settings.BACKEND_URL}/api/teams/app/{path}"


def _people(org: Organization):
    from .licensing import learners

    return learners(org)


def _admins(org: Organization):
    return AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True)


def _tz_offset(org: Organization) -> int:
    """Seconds from UTC for the company's card times (set from the admin's browser)."""
    from .admin_setup import state

    minutes = state(org).get("tz_offset_min")
    if minutes is None:
        minutes = settings.TEAMS_DEFAULT_TZ_OFFSET_MINUTES
    return int(minutes) * 60


# ---------- talking to Microsoft ----------

def token_for(org: Organization) -> str:
    """A Graph token from a saved admin login: the master admin's, else any
    admin's. If none works, the Admin Center asks an admin to sign in again."""
    from .admin_setup import state, update
    from .microsoft_auth import get_access_token_for_user
    from .slack_onboarding import primary_admin

    key = str(org.pk)
    hit = _TOKENS.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    first = primary_admin(org)
    admins = [first] if first is not None else []
    admins += [a for a in _admins(org).exclude(ms_refresh_token="") if first is None or a.pk != first.pk]
    for admin in admins:
        token = get_access_token_for_user(admin)
        if token:
            _TOKENS[key] = (time.monotonic() + _TOKEN_SECONDS, token)
            if state(org).get("teams_login_lost"):
                update(org, teams_login_lost="")
            return token
    if not state(org).get("teams_login_lost"):
        update(org, teams_login_lost=timezone.now().isoformat())
    return ""


def forget_token(org: Organization) -> None:
    _TOKENS.pop(str(org.pk), None)


def channel_id(org: Organization) -> str:
    if org.teams_welcome_channel_id:
        return org.teams_welcome_channel_id
    admin = _admins(org).exclude(teams_channel_id="").first()
    return admin.teams_channel_id if admin else ""


def _card(title: str, text: str, image: str, alt: str, button: tuple[str, str] | None) -> dict:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    if text:
        body.append({"type": "TextBlock", "text": text, "wrap": True})
    if image:
        body.append({"type": "Image", "url": image, "altText": alt or title, "size": "Stretch"})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": body, "msteams": {"width": "Full"}}
    if button:
        card["actions"] = [{"type": "Action.OpenUrl", "title": button[0], "url": button[1]}]
    return card


def post(org: Organization, title: str, text: str = "", *, image: str = "", alt: str = "",
         button: tuple[str, str] | None = None, mention: list[AIDLUser] | None = None, token: str = "") -> dict:
    """Post one card in the channel. Microsoft's short limits are retried."""
    token = token or token_for(org)
    channel = channel_id(org)
    if not token or not org.teams_team_id or not channel:
        return {"ok": False, "error": "not_connected"}
    people = [m for m in (mention or []) if m.microsoft_id and ":" not in m.microsoft_id][:MAX_MENTIONS]
    tags = " ".join(f'<at id="{i}">{escape(m.full_name or m.email)}</at>' for i, m in enumerate(people))
    payload = {
        "subject": title[:255],
        "body": {"contentType": "html", "content": f'{tags}<attachment id="aidl"></attachment>'},
        "attachments": [{"id": "aidl", "contentType": "application/vnd.microsoft.card.adaptive",
                         "content": json.dumps(_card(title, text, image, alt, button))}],
    }
    if people:
        payload["mentions"] = [{"id": i, "mentionText": m.full_name or m.email,
                                "mentioned": {"user": {"id": m.microsoft_id, "displayName": m.full_name or m.email,
                                                       "userIdentityType": "aadUser"}}}
                               for i, m in enumerate(people)]
    url = f"{GRAPH_BASE}/teams/{org.teams_team_id}/channels/{channel}/messages"
    result = {"ok": False, "error": "no_attempt"}
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=graph_headers(token), json=payload, timeout=30)
        except requests.RequestException as exc:
            logger.warning("teams post failed: %s", type(exc).__name__)
            result = {"ok": False, "error": "request_failed"}
            time.sleep(attempt + 1)
            continue
        if resp.status_code < 400:
            return {"ok": True, "message_id": (resp.json() if resp.content else {}).get("id", "")}
        logger.warning("teams post HTTP %s: %s", resp.status_code, (resp.text or "")[:300])
        result = {"ok": False, "error": f"graph_http_{resp.status_code}"}
        if resp.status_code == 401:
            forget_token(org)
        if resp.status_code not in (429, 502, 503, 504):
            break
        time.sleep(min(int(resp.headers.get("Retry-After") or attempt + 1), 10))
    return result


def post_once(org: Organization, key: str, title: str, text: str = "", **kwargs) -> bool:
    """Post unless this card is already in the channel. True if it was posted now."""
    try:
        row = TeamsPost.objects.create(organization_id=str(org.pk), key=key)
    except IntegrityError:
        return False
    result = post(org, title, text, **kwargs)
    if not result.get("ok"):
        row.delete()  # the next run tries again
        return False
    row.message_id = result.get("message_id", "")
    row.save(update_fields=["message_id"])
    return True


def email(user: AIDLUser, subject: str, text: str, path: str = "") -> bool:
    """What Slack sends as a private DM. *bold* and _italic_ marks are dropped."""
    if not user.email:
        return False
    plain = re.sub(r"[*`]", "", text)
    link = f"\n\nOpen AIDL: {app_url(path)}"
    try:
        return bool(send_mail(subject, plain + link, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True))
    except Exception as exc:  # noqa: BLE001 — a mail problem must not stop the action it reports
        logger.warning("teams email failed: %s", type(exc).__name__)
        return False


# ---------- the channel's home card ----------

def publish_home(org: Organization, token: str = "", settle: bool = False) -> dict:
    """The one card everyone opens AIDL from. Graph can't edit a card, so post the
    new one, then remove the old one. `settle`: the team may have been made seconds
    ago, and Microsoft refuses posts until it's ready, so wait and try again."""
    token = token or token_for(org)
    old = org.teams_welcome_message_id
    for wait in HOME_SETTLE_SECONDS if settle else (None,):
        result = post(org, "AI Driving Licence", f"{org.name} runs its AI Driving Licence programme here. Open AIDL "
                      "to see your next card, or to run the programme if you're an admin.",
                      button=("Open AIDL", app_url()), token=token)
        if wait is None or result.get("error") not in ("graph_http_403", "graph_http_404"):
            break
        time.sleep(wait)
    if result.get("ok") and result.get("message_id"):
        org.teams_welcome_message_id = result["message_id"]
        org.save(update_fields=["teams_welcome_message_id", "updated_at"])
        if old and old != result["message_id"]:
            from .teams_messaging import delete_channel_message

            delete_channel_message(token, team_id=org.teams_team_id, channel_id=channel_id(org), message_id=old)
    return result


def ensure_home(org: Organization) -> None:
    """Post the home card if sign-in couldn't: without it the channel is empty."""
    if org.teams_team_id and channel_id(org) and not org.teams_welcome_message_id:
        publish_home(org)


# ---------- starting the programme, reminders ----------

def start_programme(org: Organization) -> tuple[int, int]:
    """Step 5: the Welcome post, every policy card due, and an email with the link."""
    people = list(_people(org))
    post_once(org, "welcome", f"Welcome to your AI Driving Licence, {org.name}",
              "One short card at a time. About 10 minutes, and you get your AI Driving Licence at the end.",
              button=("Let's go →", app_url("me/")), mention=people)
    announce_policy(org, sorted(_due_policy(org)))
    for member in people:
        email(member, "Your AI Driving Licence",
              f"{org.name} has started its AI Driving Licence programme. It takes about 10 minutes.", "me/")
    return len(people), len(people)


def remind_everyone(org: Organization) -> tuple[int, int]:
    pending = [m for m in _people(org) if not m.licence_issued or not m.avatar_data]
    if pending:
        post(org, "Reminder: your AI Driving Licence", "You have cards waiting.",
             button=("Open AIDL →", app_url("me/")), mention=pending)
    for member in pending:
        email(member, "Reminder: your AI Driving Licence", "You still have cards to finish.", "me/")
    return len(pending), len(pending)


# ---------- policy cards ----------

def _due_policy(org: Organization) -> dict[int, dict]:
    """Policy cards to post now: straight-away cards, and dated ones from 10:00."""
    from . import aup_store

    local = timezone.now() + timedelta(seconds=_tz_offset(org))
    today = local.date().isoformat()
    return {n: c for n, c in aup_store.cards(org).items()
            if not c.get("send_on") or c["send_on"] < today or (c["send_on"] == today and local.hour >= SEND_HOUR)}


def announce_policy(org: Organization, ns: list[int]) -> int:
    from . import aup_store, card_image
    from .admin_setup import started
    from .image_cache import predraw
    from .policy_cards import SHORT

    if not started(org):
        return 0
    due = _due_policy(org)
    count = 0
    for n in ns:
        c = due.get(n)
        if c is None:
            continue
        kind = f"aup-{n}"
        predraw(card_image.png_for, kind, org)
        count += post_once(org, f"aup:{n}:{c['version']}", f"Policy card: {SHORT.get(n, c.get('title', ''))}",
                           "Read it and press I agree. It counts towards your AI Driving Licence.",
                           image=card_image.image_url(kind, org), alt=card_image.alt_text(kind, org),
                           button=("Read & agree →", app_url("me/")))
    return count


def deliver_policy_cards(org_pk, ns: list[int]) -> int:
    return announce_policy(Organization.objects.get(pk=org_pk), ns)


# ---------- awareness cards ----------

def _post_awareness(org: Organization, card: dict) -> bool:
    from . import card_image
    from .image_cache import predraw

    kind = f"pkg-{card['no']}"
    predraw(card_image.png_for, kind, org)
    return post_once(org, f"pkg:{card['no']}", card["title"], card.get("hook", ""),
                     image=card_image.image_url(kind, org), alt=card["title"],
                     button=("Got it →", app_url(f"me/cards/{card['no']}/")))


def sync_package(org: Organization, member: AIDLUser, full: bool = False) -> dict:
    """package_delivery.sync_package for Teams: the same rules (first card now,
    the rest at the plan's cadence, approved cards only, postponed ones at the
    end) with the slots saved here for dispatch() instead of handed to Slack."""
    from .card_approval import deliverable
    from .package_delivery import pause_package, post_times
    from .plans import plan_key, schedule_of

    if not member.licence_issued:
        return {"skipped": "no_licence"}
    if member.slack_left_at:
        return {"skipped": "left"}
    if full:
        pause_package(org, member)
    PackageDelivery.objects.filter(user_id=str(member.pk), status__in=[
        PackageDelivery.Status.CANCELLED, PackageDelivery.Status.FAILED]).delete()
    cards = deliverable(org)
    approved = {c["no"] for c in cards}
    PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED).exclude(
        card_no__in=approved).delete()
    rows = list(PackageDelivery.objects.filter(user_id=str(member.pk)))
    have = {r.card_no for r in rows}
    todo = [(c["position"], c) for c in cards if c["no"] not in have]
    if not todo:
        return {"sent_now": 0, "scheduled": 0}
    now = timezone.now()
    common = {"organization_id": str(org.pk), "user_id": str(member.pk), "package": plan_key(org)}
    result = {"sent_now": 0, "scheduled": 0}
    if not rows:  # the first card comes right after the licence
        position, card = todo.pop(0)
        PackageDelivery.objects.create(**common, card_no=card["no"], post_order=position, post_at=now,
                                       status=PackageDelivery.Status.SENT)
        _post_awareness(org, card)
        result["sent_now"] = 1
    start = max([now] + [r.post_at for r in rows if r.status == PackageDelivery.Status.SCHEDULED])
    for (position, card), when in zip(todo, post_times(start, len(todo), schedule_of(org), _tz_offset(org))):
        PackageDelivery.objects.create(**common, card_no=card["no"], post_order=position, post_at=when)
        result["scheduled"] += 1
    return result


def deliver_due(org: Organization, member: AIDLUser | None = None) -> int:
    """Cards whose time has come: they count as received at once, and
    each one is posted in the channel once however many people it's for."""
    from .plans import card_by_no

    rows = PackageDelivery.objects.filter(organization_id=str(org.pk), status=PackageDelivery.Status.SCHEDULED,
                                          post_at__lte=timezone.now())
    if member is not None:
        rows = rows.filter(user_id=str(member.pk))
    rows = list(rows)
    PackageDelivery.objects.filter(pk__in=[r.pk for r in rows]).update(status=PackageDelivery.Status.SENT)
    for no in sorted({r.card_no for r in rows}):
        card = card_by_no(no)
        if card is not None:
            _post_awareness(org, card)
    return len(rows)


def post_policy_awareness(org: Organization) -> int:
    """The AI policy's approved awareness cards, once somebody holds a licence."""
    from . import card_image
    from .policy_awareness import approved

    if not _people(org).filter(licence_issued=True).exists():
        return 0
    count = 0
    for c in approved(org):
        kind = f"paw-{c['id']}"
        count += post_once(org, f"paw:{c['id']}", f"AI awareness: {c['title']}", image=card_image.image_url(kind, org),
                           alt=c["title"], button=("Got it →", app_url("me/")))
    return count


# ---------- reminders and alerts ----------

def send_reminders(org: Organization, now=None) -> int:
    """One reminder per card, 7 days after it was sent, @mentioning who hasn't
    acknowledged it."""
    from .card_ack import REMIND_AFTER, _active_rows, card_title

    now = now or timezone.now()
    by_card: dict[str, list[PackageDelivery]] = {}
    for row in _active_rows(org).filter(acked_at__isnull=True, reminded_at__isnull=True, post_at__lte=now - REMIND_AFTER):
        by_card.setdefault(row.card_no, []).append(row)
    sent = 0
    for no, rows in by_card.items():
        people = list(AIDLUser.objects.filter(pk__in=[r.user_id for r in rows]))
        if not post(org, f"Reminder: {card_title(no)}", "Have you read this one? It takes about 30 seconds.",
                    button=("Got it →", app_url(f"me/cards/{no}/")), mention=people).get("ok"):
            continue
        PackageDelivery.objects.filter(pk__in=[r.pk for r in rows]).update(reminded_at=now)
        sent += len(rows)
    return sent


def remind_person(org: Organization, admin: AIDLUser, user_id: str) -> bool:
    """⏰ Remind in the Admin Center."""
    from .admin_setup import state, update
    from .card_ack import card_title, overdue_rows

    member = AIDLUser.objects.filter(pk=user_id, organization_id=str(org.pk)).first()
    row = next((r for r in overdue_rows(org) if r.user_id == user_id), None)
    if member is None or row is None:
        return False
    row.reminded_by = admin.full_name or admin.email
    row.save(update_fields=["reminded_by"])
    title = card_title(row.card_no)
    ok = post(org, f"Reminder: {title}", f"{row.reminded_by} (your AIDL admin) asked you to take a look at this card.",
              button=("Got it →", app_url(f"me/cards/{row.card_no}/")), mention=[member]).get("ok")
    email(member, f"Reminder: {title}", f"{row.reminded_by} (your AIDL admin) asked you to take a look at this card.",
          f"me/cards/{row.card_no}/")
    reminded = dict(state(org).get("ack_reminded") or {})
    reminded[user_id] = {"by": str(admin.pk), "at": timezone.now().isoformat()}
    update(org, ack_reminded=reminded)
    return bool(ok)


def after_ack(org: Organization, member: AIDLUser, title: str) -> None:
    from .card_ack import prefs_for

    for admin in _admins(org):
        if prefs_for(org, admin)["live"] == "every":
            email(admin, f"{member.full_name or member.email} acknowledged a card", f"They acknowledged: {title}", "admin/")


def send_overdue_alerts(org: Organization) -> int:
    """Once a day, to the admins who chose it. By email: names don't belong in the channel."""
    from .card_ack import admin_stats, prefs_for

    late = admin_stats(org)["overdue"]
    if not late:
        return 0
    try:
        TeamsPost.objects.create(organization_id=str(org.pk), key=f"overdue:{timezone.now().date().isoformat()}")
    except IntegrityError:
        return 0
    lines = "\n".join(f"- {p['name']}: {p['title']}" for p in late[:20])
    return sum(email(a, "AIDL: people overdue on their cards",
                     f"Reminded 3+ days ago, still not acknowledged:\n{lines}", "admin/")
               for a in _admins(org) if prefs_for(org, a)["late"] == "alert")


# ---------- who is in the programme ----------

def _new_member(org: Organization, person: dict) -> AIDLUser:
    return AIDLUser.objects.create(microsoft_id=person["user_id"], email=person.get("email", ""),
                                   full_name=person.get("full_name", ""), provider="teams",
                                   enroll_as=AIDLUser.EnrollAs.ORGANIZATION, role=AIDLUser.Role.LEARNER,
                                   organization_id=str(org.pk), organization_name=org.name,
                                   teams_team_id=org.teams_team_id, teams_channel_id=channel_id(org))


def join(org: Organization, person: dict, token: str = "") -> AIDLUser | None:
    """Someone is in the Team: make them part of the programme. None if the plan
    is full (they're taken out of the Team, as in Slack) or they're in another
    organization."""
    from .admin_setup import started
    from .package_delivery import sync_package as sync
    from .plans import can_join, usage

    member = AIDLUser.objects.filter(microsoft_id=person["user_id"]).first()
    if member is not None and member.organization_id not in ("", str(org.pk)):
        return None
    if member is None and person.get("email"):
        member = AIDLUser.objects.filter(email__iexact=person["email"], organization_id=str(org.pk)).first()
    if not can_join(org, member):
        from .microsoft_auth import remove_team_member
        from .slack_onboarding import primary_admin

        token = token or token_for(org)
        if token:
            remove_team_member(token, org.teams_team_id, person["user_id"])
        u = usage(org)
        owner = primary_admin(org)
        if owner is not None:
            email(owner, "AIDL: your plan is full",
                  f"{person.get('full_name') or 'Someone'} was added to the AIDL team, but your {u['label']} plan is "
                  f"full ({u['used']} of {u['limit']} users), so AIDL removed them again. To add them, remove someone "
                  "who hasn't earned a licence yet, or upgrade.", "admin/")
        return None
    if member is None:
        member = _new_member(org, person)
    else:
        member.organization_id, member.organization_name = str(org.pk), org.name
        fields = ["organization_id", "organization_name", "updated_at"]
        if member.slack_left_at:  # added back: the licence and cards come back with them
            member.slack_left_at = None
            fields.append("slack_left_at")
        member.save(update_fields=fields)
        if member.licence_issued and member.role != AIDLUser.Role.ADMIN:
            sync(org, member)
        return member
    if started(org):
        post(org, f"Welcome to your AI Driving Licence, {(member.full_name or 'there').split()[0]}",
             "One short card at a time, about 10 minutes.", button=("Let's go →", app_url("me/")), mention=[member])
        email(member, "Your AI Driving Licence",
              f"You've joined {org.name}'s AI Driving Licence programme. It takes about 10 minutes.", "me/")
    return member


def left(org: Organization, member: AIDLUser) -> None:
    """Out of the Team: licence suspended, cards stopped. A licence they earned
    keeps holding its seat (plans.seat_holders)."""
    from .package_delivery import pause_package

    member.slack_left_at = timezone.now()
    member.save(update_fields=["slack_left_at", "updated_at"])
    pause_package(org, member)


def sync_team(org: Organization, token: str = "") -> dict:
    """Compare the Team's members with AIDL's list. Only a list Microsoft really
    returned counts: a failed call must never take people out of the programme."""
    from .microsoft_auth import team_roster

    token = token or token_for(org)
    if not token or not org.teams_team_id:
        return {"skipped": "not_connected"}
    roster = team_roster(token, org.teams_team_id)
    if not roster:
        return {"skipped": "no_list"}
    ids = {p["user_id"] for p in roster}
    known = {u.microsoft_id: u for u in AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True)}
    out = {"joined": 0, "left": 0}
    for person in roster:
        if "guest" in person["roles"]:
            continue
        member = known.get(person["user_id"])
        if member is None or member.slack_left_at:
            out["joined"] += join(org, person, token) is not None
    for microsoft_id, member in known.items():
        if (microsoft_id not in ids and ":" not in microsoft_id and not microsoft_id.startswith("local")
                and member.role != AIDLUser.Role.ADMIN and not member.slack_left_at):
            left(org, member)
            out["left"] += 1
    return out


def remove_member(org: Organization, by: AIDLUser, user_id: str) -> tuple[bool, str]:
    from .microsoft_auth import remove_team_member

    target = AIDLUser.objects.filter(pk=user_id, organization_id=str(org.pk), is_active=True).first()
    if target is None or target.role == AIDLUser.Role.ADMIN:
        return False, "That person isn't a team member."
    if target.slack_left_at:
        return False, f"{target.full_name or target.email} was already removed."
    token = token_for(org)
    if not token or not remove_team_member(token, org.teams_team_id, target.microsoft_id):
        return False, "Microsoft didn't let AIDL remove them from the team. Please try again."
    left(org, target)
    email(target, "AIDL", f"You've been removed from {org.name}'s AIDL programme by {by.full_name or by.email}. "
                          "Your AIDL cards have stopped.")
    name = target.full_name or target.email
    if target.licence_issued:
        return True, (f"{name} was removed. Their licence {target.licence_number} is suspended and still counts "
                      "toward your plan. Adding them back restores it.")
    return True, f"{name} was removed and their seat is free again."


# ---------- the hourly job ----------

def dispatch(org: Organization) -> dict:
    """Everything that happens on its own. Each part is safe to run again."""
    from .admin_setup import started, update
    from .card_approval import remind

    update(org, teams_dispatched_at=timezone.now().isoformat())
    ensure_home(org)
    out = {"team": sync_team(org)}
    if not started(org):
        return out
    out["cards"] = deliver_due(org)
    out["policy"] = announce_policy(org, sorted(_due_policy(org)))
    out["awareness"] = post_policy_awareness(org)
    out["reminders"] = send_reminders(org)
    out["overdue"] = send_overdue_alerts(org)
    out["approval_reminders"] = remind(org)
    return out


def teams_organizations():
    return Organization.objects.filter(is_active=True, slack_team_id="").exclude(teams_team_id="")


def dispatch_all() -> dict:
    out = {}
    for org in teams_organizations():
        try:
            out[org.name] = dispatch(org)
        except Exception:  # noqa: BLE001 — one company's problem must not stop the others
            logger.exception("teams dispatch failed for org %s", org.pk)
            out[org.name] = {"error": True}
    return out


def dispatch_if_due(org: Organization) -> None:
    """Pages call this, so cards still go out if the hourly job isn't running."""
    from datetime import datetime

    from .admin_setup import state
    from .slack_modals import in_background

    last = state(org).get("teams_dispatched_at")
    try:
        if last and timezone.now() - datetime.fromisoformat(last) < DISPATCH_EVERY:
            return
    except ValueError:
        pass
    in_background(dispatch, org)
