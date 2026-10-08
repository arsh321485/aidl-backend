"""Awareness-package delivery in Slack (plans.py).

When a learner earns their licence, card 1 of their plan's package is sent
to their DM straight away and the rest are scheduled with
chat.scheduleMessage at the plan's cadence (2 a week: Monday and Thursday,
10:00 in the learner's time zone). Slack then posts them — no cron job.

sync_package() is the one entry point: it keeps what was already sent,
cancels what is still scheduled, and re-schedules the cards the learner
hasn't had yet. Call it on licence issue, when a member is added back, and
when the organization's plan changes. pause_package() cancels the future
cards when a member leaves the channel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from . import slack_client
from .models import AIDLUser, Organization, PackageDelivery
from .plans import package_cards, plan_of

logger = logging.getLogger(__name__)

# Weekdays (Mon=0) cards go out on, by cards per week.
_WEEKDAYS = {1: (0,), 2: (0, 3), 3: (0, 2, 4), 4: (0, 1, 3, 4)}
_POST_HOUR = 10
# Slack only schedules messages up to 120 days ahead.
_MAX_AHEAD = timedelta(days=119)


def post_times(start: datetime, count: int, per_week: int, tz_offset: int = 0) -> list[datetime]:
    """The next `count` delivery slots after `start` (UTC), at 10:00 local."""
    days = _WEEKDAYS.get(per_week, _WEEKDAYS[2])
    offset = timedelta(seconds=tz_offset)
    local = (start + offset).replace(hour=_POST_HOUR, minute=0, second=0, microsecond=0)
    times: list[datetime] = []
    while len(times) < count:
        utc = local - offset
        if local.weekday() in days and utc > start + timedelta(hours=1):
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
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📘 {card['title']}", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"Card {position} of {total}  ·  {card['category']}  ·  {card['type']}"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{card['hook']}*\n\n{card['message']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"👉 {card['action']}"}},
    ]
    if card.get("local_link"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _local_line(org, card)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
        f"AIDL · {org.name} · {plan_of(org)['label']} plan"}]})
    return blocks


def _mark_sent(member: AIDLUser) -> None:
    PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED,
                                   post_at__lte=timezone.now()).update(status=PackageDelivery.Status.SENT)


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


def sync_package(org: Organization, member: AIDLUser) -> dict:
    """Make the learner's scheduled cards match their plan. Returns
    {'sent_now': n, 'scheduled': n} (or {'skipped': reason})."""
    if not member.licence_issued:
        return {"skipped": "no_licence"}
    if member.slack_left_at:
        return {"skipped": "left"}
    slack_user = _slack_user_id(member)
    token = slack_client.bot_token(org)
    if not slack_user or not token:
        return {"skipped": "not_slack"}

    pause_package(org, member)
    PackageDelivery.objects.filter(user_id=str(member.pk), status__in=[
        PackageDelivery.Status.CANCELLED, PackageDelivery.Status.FAILED]).delete()
    sent = set(PackageDelivery.objects.filter(user_id=str(member.pk)).values_list("card_no", flat=True))
    cards = package_cards(org)
    total = len(cards)
    todo = [(i, c) for i, c in enumerate(cards, 1) if c["no"] not in sent]
    if not todo:
        return {"sent_now": 0, "scheduled": 0}

    dm = slack_client.slack_api("conversations.open", token, json={"users": slack_user})
    channel = ((dm.get("channel") or {}).get("id")) or slack_user
    info = slack_client.slack_api("users.info", token, params={"user": slack_user})
    tz_offset = int(((info.get("user") or {}).get("tz_offset")) or 0)
    plan = plan_of(org)
    now = timezone.now()
    common = {"organization_id": str(org.pk), "user_id": str(member.pk), "package": plan["package"],
              "slack_channel_id": channel}
    result = {"sent_now": 0, "scheduled": 0}

    if not sent:  # first card right after the licence
        position, card = todo.pop(0)
        res = slack_client.slack_api("chat.postMessage", token, json={
            "channel": channel, "text": f"AIDL card: {card['title']}", "blocks": card_blocks(org, card, position, total)})
        PackageDelivery.objects.create(**common, card_no=card["no"], post_order=position, post_at=now,
                                       status=PackageDelivery.Status.SENT if res.get("ok") else PackageDelivery.Status.FAILED,
                                       error=res.get("error", "") or "")
        result["sent_now"] = 1

    for (position, card), when in zip(todo, post_times(now, len(todo), plan["cards_per_week"], tz_offset)):
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


def sync_organization(org: Organization) -> dict:
    """After a plan change: re-schedule every licensed learner's cards."""
    from .licensing import learners

    out = {}
    for member in learners(org).filter(licence_issued=True):
        out[member.email or str(member.pk)] = sync_package(org, member)
    return out


def progress(member: AIDLUser) -> dict:
    _mark_sent(member)
    rows = PackageDelivery.objects.filter(user_id=str(member.pk))
    return {"sent": rows.filter(status=PackageDelivery.Status.SENT).count(),
            "scheduled": rows.filter(status=PackageDelivery.Status.SCHEDULED).count()}
