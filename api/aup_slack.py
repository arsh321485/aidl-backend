"""Slack views of the AI policy as it stands (the policy cards sent so far,
aup_store.py). The admin builds it card by card in policy_cards_slack.py.

    view_modal(org)              "View policy": every card sent so far
    employee_blocks(member, org) the older dashboard's AUP tab: the next card
                                 to agree to
"""

from __future__ import annotations

from . import aup_store
from .models import AIDLUser, Organization


def _text(t: str) -> dict:
    return {"type": "plain_text", "text": t[:150], "emoji": True}


def _md(t: str) -> dict:
    return {"type": "mrkdwn", "text": t}


def _clip(text: str, n: int = 2900) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _point_text(point: dict) -> str:
    return "\n".join(f"•  {c['text']}" for c in point["clauses"]) or "_Nothing on this card._"


def aup_blocks(aup: dict) -> list:
    org = aup["organization"]
    n = len(aup["points"])
    blocks = [{"type": "header", "text": _text("📄 AI policy")},
              {"type": "context", "elements": [_md(f"*{org['name']}*  ·  {n} policy card{'s' if n != 1 else ''} "
                                                   "sent so far")]}]
    for p in aup["points"]:
        blocks += [{"type": "divider"},
                   {"type": "section", "text": _md(_clip(f"*{p['no']} · {p['title']}*\n{_point_text(p)}"))}]
    return blocks


def view_modal(org: Organization) -> dict:
    aup = aup_store.current(org)
    blocks = aup_blocks(aup) if aup else [{"type": "section", "text": _md(
        "No policy cards sent yet — use *📌 Add a policy card* in the Admin Center.")}]
    return {"type": "modal", "callback_id": "aidl_aup_view", "title": _text("Your AI policy"), "close": _text("Close"),
            "blocks": blocks[:100]}


def employee_blocks(member: AIDLUser, org: Organization) -> list:
    if not aup_store.required_points(org):
        return [{"type": "header", "text": _text("📄 AI policy")},
                {"type": "section", "text": _md(f"⏳ *{org.name}* hasn't sent any policy cards yet. They'll appear "
                                                "here when it does.")}]
    done = aup_store.points_accepted(member, org)
    total = len(aup_store.required_points(org))
    n = aup_store.next_point(member, org)
    bar = "▰" * len(done) + "▱" * (total - len(done))
    if n is None:
        return [{"type": "header", "text": _text("📄 AI policy")},
                {"type": "section", "text": _md(f"✅ *You've agreed to all {total} policy cards* from {org.name}.")},
                {"type": "context", "elements": [_md(bar)]}]
    point = aup_store.point(org, n)
    return [
        {"type": "header", "text": _text(f"📄 AI policy — {point['title']}")},
        {"type": "context", "elements": [_md(f"*{org.name}*  ·  {bar}  {len(done)} of {total} agreed")]},
        {"type": "section", "text": _md(_clip(f"*{point['title']}*\n\n{_point_text(point)}"))},
        {"type": "actions", "block_id": "aidl_aup_ack", "elements": [
            {"type": "button", "text": _text("✅ I agree"), "action_id": "aidl_ack_aup",
             "value": aup_store.point_item(n), "style": "primary"}]},
        {"type": "context", "elements": [_md("Agreeing to every policy card, the Highway Code and the Traffic Light "
                                             "Check earns your AI Driving License.")]},
    ]
