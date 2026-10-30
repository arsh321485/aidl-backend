"""Policy cards: the AI policy, one short card per topic (design
E:\\AIDL Policy Cards (Slack).html).

The admin picks a topic (one of the 14 AUP points), checks its questions —
the recommended answers are already picked — and approves; the card goes
to the team straight away or at 10:00 (each person's time zone) on a chosen
day. Employees press "I agree" on each card; agreeing to every card sent so
far, the Traffic Light Check and the Highway Code earns the licence.

    lines(n, answers)           the card's sentences (aup_builder wording)
    problems(n, answers)        what's missing on that topic
    approve(org, admin, n, …)   save the card (aup_store) and deliver it
    deliver_to(org, member)     everything due that the person hasn't got yet

Delivery: DMs now for cards due today, chat.scheduleMessage for later days
(ids kept on the card so a new version can cancel them). Nothing is sent
before the programme has started (Admin Center step 5); starting it, and
people who start later, get every card that's due.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from . import aup_builder, aup_store, slack_client
from .models import AIDLUser, Organization

logger = logging.getLogger(__name__)
TOPICS = range(1, 15)
SHORT = {1: "Who this applies to", 2: "Monitoring", 3: "Privacy on company devices", 4: "Installing software",
         5: "Passwords & sign-in", 6: "Company data", 7: "AI tools at work", 8: "Email, chat & social media",
         9: "Reporting a problem", 10: "Working remotely", 11: "Who owns your work", 12: "Leaving the company",
         13: "If rules are broken", 14: "Agreeing & reviews"}
ICONS = {1: "👥", 2: "👀", 3: "🗂", 4: "💻", 5: "🔑", 6: "🔒", 7: "🤖", 8: "💬", 9: "🚨", 10: "🏠", 11: "©️",
         12: "🚪", 13: "⚖️", 14: "✍️"}
SEND_HOUR = 10
MAX_DAYS_AHEAD = 110  # Slack schedules messages up to 120 days ahead
POPULAR_AI_TOOLS = ["ChatGPT (Free / Plus)", "ChatGPT Team", "ChatGPT Enterprise", "Microsoft 365 Copilot",
                    "Microsoft Copilot", "Claude", "Google Gemini", "GitHub Copilot", "Perplexity", "Grammarly",
                    "Notion AI", "Canva Magic Studio", "Zoom AI Companion", "Slack AI", "DeepL", "Midjourney",
                    "Otter.ai", "Fireflies.ai"]
APPROVED_AI = "7.1.b"  # "Allowed — approved tools only" → the approved tools list


def questions(n: int, answers: dict) -> list[dict]:
    """The topic's questions that apply, given the answers so far."""
    return aup_builder.visible_questions(answers, n)


def lines(n: int, a: dict) -> list[str]:
    """Card n's sentences from {answers, fills, tools} — only this topic's
    questions (the organization profile is not part of any card)."""
    try:
        aup = aup_builder.generate(a["answers"], a["fills"], strict=False)
    except Exception:  # noqa: BLE001 — a half-typed answer must not break the screen
        return []
    pt = next((p for p in aup["points"] if p["no"] == n), None)
    out = []
    for c in (pt or {}).get("clauses") or []:
        if not c.get("qid") or aup_builder.question(c["qid"])["section"] != n:
            continue
        out.append(c["text"])
        if c["qid"] == "7.1" and a["answers"].get("7.1") == APPROVED_AI:
            out.append(f"Approved AI tools: {_join(a['tools'])}." if a["tools"] else "Approved AI tools: ask your AIDL admin.")
    return out


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def problems(n: int, a: dict) -> dict[str, str]:
    """{question id (or "tools"): message} for this topic."""
    on_card = {q["id"] for q in questions(n, a["answers"])}
    out = {qid: msg for qid, msg in aup_builder.validate(a["answers"], a["fills"]).items() if qid in on_card}
    if n == 7 and a["answers"].get("7.1") == APPROVED_AI and not a["tools"]:
        out["tools"] = "Pick or paste at least one AI tool."
    return out


def statuses(org: Organization) -> dict[int, dict]:
    """{point: sent / scheduled} for every approved card — one database
    query for all the "n agreed" counts (Slack waits only 3 seconds)."""
    cards = aup_store.cards(org)
    counts = agreed_counts(org, cards)
    out = {}
    for n, c in cards.items():
        if not aup_store.is_due(c):
            out[n] = {"state": "scheduled", "send_on": c["send_on"]}
        else:
            out[n] = {"state": "sent", "agreed": counts.get(n, 0), "version": c["version"]}
    return out


def status(org: Organization, n: int) -> dict:
    """For the topic list and Admin Center: sent / scheduled / not yet."""
    return statuses(org).get(n) or {"state": "new"}


def agreed_counts(org: Organization, cards: dict | None = None) -> dict[int, int]:
    from .admin_setup import _slack_learners
    from .models import Acknowledgement

    cards = aup_store.cards(org) if cards is None else cards
    want = {aup_store.point_item(n): c["version"] for n, c in cards.items()}
    if not want:
        return {}
    ids = [str(pk) for pk in _slack_learners(org).values_list("pk", flat=True)]
    out: dict[int, int] = {}
    for item, ver in Acknowledgement.objects.filter(user_id__in=ids, item__in=list(want)).values_list("item", "version"):
        if want.get(item) == ver:
            out[int(item[4:])] = out.get(int(item[4:]), 0) + 1
    return out


def agreed_count(org: Organization, n: int) -> int:
    return agreed_counts(org).get(n, 0)


def summary(org: Organization) -> list[dict]:
    """Approved cards in point order, with their state."""
    st = statuses(org)
    return [{"no": n, "title": SHORT[n], "icon": ICONS[n], **st[n]} for n in sorted(st)]


# ---------- approving and sending ----------

def approve(org: Organization, admin: AIDLUser | None, n: int, a: dict, send_on: str = "", *,
            card_lines: list[str] | None = None, source: str = "questions", file_name: str = "",
            deliver: bool = True) -> dict:
    """Save card n and send (or schedule) it. `a` = {answers, fills, tools}
    (saved for the next edit); `card_lines` overrides the generated text
    (uploaded AUPs)."""
    if a:
        aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
    old = aup_store.card(org, n)
    if old:
        _cancel_scheduled(org, old)
    text = card_lines if card_lines is not None else lines(n, a)
    c = aup_store.save_card(org, n, SHORT[n], text, by=admin, send_on=send_on, source=source, file_name=file_name)
    from .admin_setup import started
    from .slack_modals import in_background

    if deliver and started(org):
        in_background(deliver_cards, org.pk, [n])
    return c


def deliver_cards(org_pk, ns: list[int]) -> int:
    """Send these cards to everyone in the programme (or schedule them)."""
    from .admin_setup import _slack_learners

    org = Organization.objects.get(pk=org_pk)
    count = 0
    for member in _slack_learners(org):
        for n in ns:
            count += _send(org, member, n)
    return count


def deliver_to(org: Organization, member: AIDLUser) -> int:
    """A person starts (or the programme starts): every approved card they
    haven't agreed to — due ones now, later ones scheduled."""
    done = aup_store.points_accepted(member, org)
    return sum(_send(org, member, n) for n in sorted(aup_store.cards(org)) if n not in done)


def _send(org: Organization, member: AIDLUser, n: int) -> int:
    from . import employee_flow

    c = aup_store.card(org, n)
    slack_id = employee_flow._slack_id(member)
    if c is None or not slack_id:
        return 0
    payload = employee_flow.policy_card(member, org, n)
    when = _post_at(org, slack_id, c.get("send_on") or "")
    if when is None:
        r = employee_flow.post(org, member, payload)
        return 1 if r.get("ok") or r.get("fallback_ok") else 0
    token = slack_client.bot_token(org)
    dm = slack_client.slack_api("conversations.open", token, json={"users": slack_id})
    channel = (dm.get("channel") or {}).get("id", "")
    if not channel:
        return 0
    from .image_cache import predraw
    from . import card_image

    predraw(card_image.png_for, f"aup-{n}", org)
    r = slack_client.slack_api("chat.scheduleMessage", token, json={
        "channel": channel, "post_at": int(when.timestamp()), "text": payload["text"], "blocks": payload["blocks"],
        "attachments": payload.get("attachments") or []})
    if r.get("ok"):
        sched = dict((aup_store.card(org, n) or {}).get("scheduled") or {})
        sched[str(member.pk)] = [channel, r.get("scheduled_message_id", "")]
        aup_store.set_scheduled(org, n, sched)
        return 1
    logger.info("policy card %s schedule failed: %s", n, r.get("error"))
    return 0


def _post_at(org: Organization, slack_id: str, send_on: str) -> datetime | None:
    """10:00 on send_on in the person's time zone, or None = now."""
    if not send_on:
        return None
    info = slack_client.slack_api("users.info", slack_client.bot_token(org), params={"user": slack_id})
    offset = timedelta(seconds=int(((info.get("user") or {}).get("tz_offset")) or 0))
    local = datetime.combine(date.fromisoformat(send_on), datetime.min.time()).replace(hour=SEND_HOUR)
    when = (local - offset).replace(tzinfo=dt_timezone.utc)
    return when if when > timezone.now() + timedelta(minutes=2) else None


def _cancel_scheduled(org: Organization, c: dict) -> None:
    token = slack_client.bot_token(org)
    for channel, sid in (c.get("scheduled") or {}).values():
        if sid:
            slack_client.slack_api("chat.deleteScheduledMessage", token, json={"channel": channel,
                                                                              "scheduled_message_id": sid})


def check_day(value: str) -> str:
    """'' if the chosen day is fine, else why not."""
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return "Pick a day."
    today = timezone.localdate()
    if day < today:
        return "Pick today or a later day."
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        return f"Pick a day within the next {MAX_DAYS_AHEAD} days."
    return ""
