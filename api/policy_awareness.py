"""Awareness cards made from the general parts of the old AUP questions
(the manager's cuts, Oct 2026): what never goes into AI tools, what's not
allowed on company systems, locking your screen, clean desk and printing.

Ready-made — no questions. Admins approve them in Card approval
(card_approval keys "paw:<id>"); approved cards go to each employee once
their licence is issued (or at once to people already licensed), as an
image (card_image "paw-<id>") with "Got it" (Acknowledgement "paw_<id>").
"""

from __future__ import annotations

import re

from . import aup_builder, slack_client
from .models import Acknowledgement, AIDLUser, Organization

_SPECIFY = re.compile(r"\s*\((?:please\s+)?specify[^)]*\)\s*", re.I)


def _options(qid: str) -> list[str]:
    return [_SPECIFY.sub("", o["text"]).strip() for o in aup_builder.question(qid)["options"]
            if not o["text"].lower().startswith("other")]


def cards() -> list[dict]:
    """[{id, title, lines}] in the order they're sent."""
    never = [f"Never put {t.lower() if t.startswith('Any ') else aup_builder._mid_sentence(t)} into an AI tool."
             for t in _options("7.3")]
    banned = _options("4.1")
    half = (len(banned) + 1) // 2
    return [
        {"id": "never-ai", "icon": "🔒", "title": "Never paste into AI", "lines": never + [
            "Not sure? Treat it as private and leave it out."]},
        {"id": "not-allowed-1", "icon": "🚫", "title": "Not allowed on company systems (1 of 2)", "lines": banned[:half]},
        {"id": "not-allowed-2", "icon": "🚫", "title": "Not allowed on company systems (2 of 2)", "lines": banned[half:]},
        {"id": "screen-lock", "icon": "🖥", "title": "Lock your screen", "lines": [
            "Lock your screen every time you step away — Windows + L, or Ctrl + Cmd + Q on a Mac.",
            "Your screen also locks itself after a few minutes. Don't turn that off."]},
        {"id": "clean-desk", "icon": "🗄", "title": "Clean desk & printing", "lines": _options("6.6")},
    ]


def by_id(card_id: str) -> dict | None:
    return next((c for c in cards() if c["id"] == card_id), None)


def key(card_id: str) -> str:
    return f"paw:{card_id}"


def ack_item(card_id: str) -> str:
    return f"paw_{card_id}"


def is_ack_item(item: str) -> bool:
    return item.startswith("paw_") and by_id(item[4:]) is not None


def approved(org: Organization) -> list[dict]:
    from .card_approval import APPROVED, _rows

    rows = _rows(org)
    return [c for c in cards() if (rows.get(key(c["id"])) is not None and rows[key(c["id"])].status == APPROVED)]


# ---------- delivery ----------

def blocks(org: Organization, c: dict, acked: bool = False) -> list:
    from . import card_image
    from .image_cache import predraw

    kind = f"paw-{c['id']}"
    predraw(card_image.png_for, kind, org)
    action = ({"type": "context", "elements": [{"type": "mrkdwn", "text": "✅ Acknowledged"}]} if acked else
              {"type": "actions", "block_id": "aidl_paw", "elements": [
                  {"type": "button", "text": {"type": "plain_text", "text": "✅ Got it", "emoji": True},
                   "action_id": "aidl_paw_ack", "value": c["id"], "style": "primary"},
                  {"type": "button", "text": {"type": "plain_text", "text": "📚 My cards", "emoji": True},
                   "action_id": "aidl_my_cards", "value": "0"}]})
    return [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*AI awareness · {c['icon']} {c['title']}*"}]},
        {"type": "image", "image_url": card_image.image_url(kind, org), "alt_text": f"{c['title']}. " + " ".join(c["lines"])},
        action,
    ]


def _sent(org: Organization) -> dict:
    from .admin_setup import state

    return state(org).get("paw_sent") or {}


def send_to(org: Organization, member: AIDLUser) -> int:
    """Approved cards this person hasn't had yet (after their licence)."""
    from .admin_setup import update
    from .employee_flow import _slack_id

    slack_id = _slack_id(member)
    if not slack_id:
        return 0
    sent = _sent(org)
    have = set(sent.get(str(member.pk)) or [])
    token = slack_client.bot_token(org)
    count = 0
    for c in approved(org):
        if c["id"] in have:
            continue
        r = slack_client.send_dm(token, org, slack_id, text=f"AI awareness: {c['title']}", blocks=blocks(org, c))
        if r.get("ok") or r.get("fallback_ok"):
            have.add(c["id"])
            count += 1
    if count:
        sent[str(member.pk)] = sorted(have)
        update(org, paw_sent=sent)
    return count


def send_to_licensed(org_pk) -> int:
    """After an approval: everyone already licensed gets the new card(s)."""
    from .admin_setup import _slack_learners

    org = Organization.objects.get(pk=org_pk)
    return sum(send_to(org, m) for m in _slack_learners(org).filter(licence_issued=True))


def acknowledge(org: Organization, member: AIDLUser, card_id: str, channel: str, ts: str) -> None:
    from django.db import IntegrityError

    c = by_id(card_id)
    if c is None:
        return
    try:
        Acknowledgement.objects.create(organization_id=str(org.pk), user_id=str(member.pk), item=ack_item(card_id),
                                       version="v1")
    except IntegrityError:
        pass
    if channel and ts:
        slack_client.slack_api("chat.update", slack_client.bot_token(org), json={
            "channel": channel, "ts": ts, "text": f"AI awareness: {c['title']}", "blocks": blocks(org, c, acked=True)})
