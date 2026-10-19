"""Awareness-package delivery in Slack (plans.py).

When a learner earns their licence, card 1 of their plan's package is sent
to their DM straight away and the rest are scheduled with
chat.scheduleMessage at the plan's cadence (plans.py: Trial 1 a month,
Basic Mon + Thu, Enterprise 4 a week on the admin's days), in the learner's
time zone. Slack posts them. Slack only accepts messages up to 120 days
ahead, so later cards are saved and handed to Slack by schedule_due() —
run `python manage.py schedule_package_cards` daily.

Only cards an admin approved are sent (card_approval.py). A card still
waiting at its slot is postponed: the slot goes to the next approved card,
and the waiting one is added after the learner's last scheduled card once
it's approved.

sync_package() is the one entry point. Incremental (default): keeps what is
sent or scheduled and adds the approved cards the learner hasn't got yet —
on licence issue, after approvals, when a member is added back. full=True:
cancels everything scheduled and schedules again from now — when the plan
changes. pause_package() cancels the future cards when a member leaves the
channel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from . import slack_client
from .models import AIDLUser, Organization, PackageDelivery
from .plans import plan_key, plan_of, schedule_of

logger = logging.getLogger(__name__)

# Weekdays (Mon=0) cards go out on, by cards per week.
_WEEKDAYS = {1: (0,), 2: (0, 3), 3: (0, 2, 4), 4: (0, 1, 3, 4)}
_POST_HOUR = 10
# Slack only schedules messages up to 120 days ahead.
_MAX_AHEAD = timedelta(days=119)


def post_times(start: datetime, count: int, schedule, tz_offset: int = 0) -> list[datetime]:
    """The next `count` delivery slots after `start` (UTC), local time.
    `schedule` is plans.schedule_of(): {"days": (Mon=0 …), "hour"} or
    {"monthly": True, "hour"} (first Monday of each month); a plain number
    means that many cards a week."""
    if isinstance(schedule, int):
        schedule = {"days": _WEEKDAYS.get(schedule, _WEEKDAYS[2]), "hour": _POST_HOUR}
    hour = int(schedule.get("hour", _POST_HOUR))
    offset = timedelta(seconds=tz_offset)
    local = (start + offset).replace(hour=hour, minute=0, second=0, microsecond=0)
    times: list[datetime] = []
    while len(times) < count:
        utc = local - offset
        if schedule.get("monthly"):
            ok = local.weekday() == 0 and local.day <= 7  # first Monday of the month
        else:
            ok = local.weekday() in schedule["days"]
        if ok and utc > start + timedelta(hours=1):
            times.append(utc)
        local += timedelta(days=1)
    return times


def _slack_user_id(member: AIDLUser) -> str:
    return member.microsoft_id.split(":")[-1] if member.microsoft_id.startswith("slack:") else ""


def _local_line(org: Organization, card: dict) -> str:
    """The workbook flags cards that point at the company's own policy,
    registry, channel or team — fill them from what AIDL knows."""
    from .aup import generate_aup
    from .org_policy import get_answers, policy_effects

    no = card["no"]
    if no == "002":
        return "📄 Your company's AI Acceptable Use Policy is in the *AUP* tab of your AIDL dashboard."
    if no in ("003", "026"):
        aup = generate_aup(org)
        names = [a["name"] for a in aup["allowed_ai"] + aup["allowed_it"]]
        listed = ", ".join(names[:8]) if names else "none yet — ask your AIDL admin"
        return f"✅ *Approved at {org.name}:* {listed}\nFull list with data allowed: *AUP* tab of your AIDL dashboard."
    if no == "050":
        report_to = policy_effects(get_answers(org)).get("report_to")
        if report_to:
            return f"🚨 At {org.name}, report AI mistakes to *{report_to}*."
    return f"🔗 Ask your AIDL admin for {org.name}'s link or contact for this."


def card_blocks(org: Organization, card: dict, position: int, total: int) -> list:
    """The awareness card as an image (card_image.py — what the admin
    approved), plus the company-specific line and Request an app."""
    from . import card_image

    kind = f"pkg-{card['no']}"
    blocks = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"📘 *Card {position} of {total}*  ·  {card['category']}  ·  {card['type']}"}]},
        {"type": "image", "image_url": card_image.image_url(kind, org), "alt_text": card_image.alt_text(kind)},
    ]
    if card.get("local_link"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _local_line(org, card)}})
    blocks += [
        {"type": "actions", "block_id": "aidl_card_footer", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🛣 Request an app", "emoji": True},
             "action_id": "aidl_app_request_open", "value": "card"}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"AIDL · {org.name} · {plan_of(org)['label']} plan"}]},
    ]
    return blocks


def _mark_sent(member: AIDLUser) -> None:
    """Cards Slack has posted by now (only those handed to Slack)."""
    (PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED,
                                    post_at__lte=timezone.now()).exclude(scheduled_message_id="")
     .update(status=PackageDelivery.Status.SENT))


def pause_package(org: Organization, member: AIDLUser) -> int:
    """Cancel the cards still waiting to be posted. Returns how many."""
    _mark_sent(member)
    token = slack_client.bot_token(org)
    rows = list(PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED))
    for row in rows:
        if row.scheduled_message_id and token:
            slack_client.slack_api("chat.deleteScheduledMessage", token, json={
                "channel": row.slack_channel_id, "scheduled_message_id": row.scheduled_message_id})
        row.status = PackageDelivery.Status.CANCELLED
        row.save(update_fields=["status"])
    return len(rows)


def sync_package(org: Organization, member: AIDLUser, full: bool = False) -> dict:
    """Schedule the learner's approved cards. Returns {'sent_now': n,
    'scheduled': n} (or {'skipped': reason})."""
    from .card_approval import deliverable, sequence

    if not member.licence_issued:
        return {"skipped": "no_licence"}
    if member.slack_left_at:
        return {"skipped": "left"}
    slack_user = _slack_user_id(member)
    token = slack_client.bot_token(org)
    if not slack_user or not token:
        return {"skipped": "not_slack"}

    if full:
        pause_package(org, member)
    else:
        _mark_sent(member)
    PackageDelivery.objects.filter(user_id=str(member.pk), status__in=[
        PackageDelivery.Status.CANCELLED, PackageDelivery.Status.FAILED]).delete()
    total = len(sequence(org))
    cards = deliverable(org)
    approved = {c["no"] for c in cards}
    # a card that was skipped after it was scheduled
    for row in PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED):
        if row.card_no not in approved:
            if row.scheduled_message_id and token:
                slack_client.slack_api("chat.deleteScheduledMessage", token, json={
                    "channel": row.slack_channel_id, "scheduled_message_id": row.scheduled_message_id})
            row.delete()
    rows = list(PackageDelivery.objects.filter(user_id=str(member.pk)))
    sent = {r.card_no for r in rows}
    todo = [(c["position"], c) for c in cards if c["no"] not in sent]
    if not todo:
        return {"sent_now": 0, "scheduled": 0}

    dm = slack_client.slack_api("conversations.open", token, json={"users": slack_user})
    channel = ((dm.get("channel") or {}).get("id")) or slack_user
    info = slack_client.slack_api("users.info", token, params={"user": slack_user})
    tz_offset = int(((info.get("user") or {}).get("tz_offset")) or 0)
    plan = plan_of(org)
    now = timezone.now()
    common = {"organization_id": str(org.pk), "user_id": str(member.pk), "package": plan_key(org),
              "slack_channel_id": channel}
    result = {"sent_now": 0, "scheduled": 0}

    if not rows:  # first card right after the licence
        position, card = todo.pop(0)
        res = slack_client.slack_api("chat.postMessage", token, json={
            "channel": channel, "text": f"AIDL card: {card['title']}", "blocks": card_blocks(org, card, position, total)})
        PackageDelivery.objects.create(**common, card_no=card["no"], post_order=position, post_at=now,
                                       status=PackageDelivery.Status.SENT if res.get("ok") else PackageDelivery.Status.FAILED,
                                       error=res.get("error", "") or "")
        result["sent_now"] = 1

    # Postponed cards follow the learner's last scheduled card.
    start = max([now] + [r.post_at for r in rows if r.status == PackageDelivery.Status.SCHEDULED])
    for (position, card), when in zip(todo, post_times(start, len(todo), schedule_of(org), tz_offset)):
        row = PackageDelivery(**common, card_no=card["no"], post_order=position, post_at=when)
        if when - now <= _MAX_AHEAD:
            res = slack_client.slack_api("chat.scheduleMessage", token, json={
                "channel": channel, "post_at": int(when.astimezone(dt_timezone.utc).timestamp()),
                "text": f"AIDL card: {card['title']}", "blocks": card_blocks(org, card, position, total)})
            if res.get("ok"):
                row.scheduled_message_id = res.get("scheduled_message_id", "")
                result["scheduled"] += 1
            else:
                row.status = PackageDelivery.Status.FAILED
                row.error = res.get("error", "") or "schedule failed"
        row.save()
    return result


def schedule_due(org: Organization) -> int:
    """Hand Slack the saved cards that are now within its 120-day window.
    Returns how many were scheduled."""
    token = slack_client.bot_token(org)
    if not token:
        return 0
    from .card_approval import sequence

    seq = sequence(org)
    cards = {c["no"]: c for c in seq if c["approval"] == "approved"}
    total = len(seq)
    limit = timezone.now() + _MAX_AHEAD
    done = 0
    for row in PackageDelivery.objects.filter(organization_id=str(org.pk), status=PackageDelivery.Status.SCHEDULED,
                                              scheduled_message_id="", post_at__lte=limit):
        card = cards.get(row.card_no)
        if card is None:
            continue
        if row.post_at <= timezone.now() + timedelta(minutes=2):  # missed its slot → a few minutes from now
            row.post_at = timezone.now() + timedelta(minutes=5)
        res = slack_client.slack_api("chat.scheduleMessage", token, json={
            "channel": row.slack_channel_id, "post_at": int(row.post_at.astimezone(dt_timezone.utc).timestamp()),
            "text": f"AIDL card: {card['title']}", "blocks": card_blocks(org, card, row.post_order, total)})
        if res.get("ok"):
            row.scheduled_message_id = res.get("scheduled_message_id", "")
            done += 1
        else:
            row.status = PackageDelivery.Status.FAILED
            row.error = res.get("error", "") or "schedule failed"
        row.save()
    return done


def sync_organization(org: Organization, full: bool = True) -> dict:
    """Every licensed learner: after a plan change (full: re-schedule from
    now) or after approvals (incremental: add the newly approved cards)."""
    from .licensing import learners

    out = {}
    for member in learners(org).filter(licence_issued=True):
        out[member.email or str(member.pk)] = sync_package(org, member, full=full)
    return out


def progress(member: AIDLUser) -> dict:
    _mark_sent(member)
    rows = PackageDelivery.objects.filter(user_id=str(member.pk))
    return {"sent": rows.filter(status=PackageDelivery.Status.SENT).count(),
            "scheduled": rows.filter(status=PackageDelivery.Status.SCHEDULED).count()}
