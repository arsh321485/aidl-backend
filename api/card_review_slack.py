"""Slack popup: Admin Center step 4 "Review & approve cards" (card_approval.py).

    Onboarding cards   🚦 Traffic Light · 📖 Highway Code · 📄 AUP  [👁 Preview] [✅ Approve]
    Awareness cards    counts · [✅ Approve all waiting]
      Month n of N     [← Month n-1] [Month n+1 →] [✅ Approve month n]
        one line per card with a ⋯ menu: Preview · Approve · Skip · Swap
    Extra reference cards (optional) → the old Send Cards popup

Button / menu clicks (action ids aidl_crv_*) redraw the popup in place;
Preview pushes a popup with the card image as the team will see it.
"""

from __future__ import annotations

import json

from . import card_approval as ca

CALLBACK = "aidl_cards_review"
PREVIEW = "aidl_card_preview"
ICON = {ca.APPROVED: "✅", ca.SKIPPED: "⏭", ca.WAITING: "⏳"}
WORD = {ca.APPROVED: "Approved", ca.SKIPPED: "Skipped", ca.WAITING: "Waiting for approval"}


def _t(text: str) -> dict:
    return {"type": "plain_text", "text": text[:75], "emoji": True}


def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _btn(text, action_id, value, style=""):
    b = {"type": "button", "text": _t(text), "action_id": action_id, "value": str(value)}
    if style:
        b["style"] = style
    return b


def _opt(text, value):
    return {"text": _t(text), "value": value}


def review_view(org, month: int = 1, notice: str = "") -> dict:
    from .plans import PACKAGE_LABELS, plan_key

    total_months = ca.months(org)
    month = min(max(1, month), total_months)
    blocks = [
        {"type": "section", "text": _md("*Nothing reaches your team until it's approved.* Approved cards go out on "
                                        "schedule; a card that's still waiting is postponed and follows once you "
                                        "approve it.")},
    ]
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})

    blocks += [{"type": "divider"}, {"type": "section", "text": _md("*Onboarding cards* — needed before the programme "
                                                                     "starts")}]
    for row in ca.onboarding(org):
        if not row["exists"]:
            blocks.append({"type": "section", "text": _md(f"{row['label']}  ·  _create your AUP first (step 1)_")})
            continue
        who = f" by {row['by']}" if row["status"] == ca.APPROVED and row["by"] else ""
        blocks.append({"type": "section", "text": _md(f"{ICON[row['status']]}  *{row['label']}*  ·  "
                                                      f"{WORD[row['status']]}{who}")})
        blocks.append({"type": "actions", "block_id": f"crv_onb_{row['item']}", "elements": [
            _btn("👁 Preview", "aidl_crv_preview", f"onb|{row['item']}"),
            *([_btn("✅ Approve", "aidl_crv_approve_onb", row["item"], "primary")]
              if row["status"] != ca.APPROVED else []),
        ]})

    n = ca.counts(org)
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": _md(f"*Awareness cards — {PACKAGE_LABELS[plan_key(org)]}* ({n['total']})\n"
                                        f"✅ {n['approved']} approved  ·  ⏳ {n['waiting']} waiting  ·  "
                                        f"⏭ {n['skipped']} skipped")},
    ]
    if n["waiting"]:
        blocks.append({"type": "actions", "block_id": "crv_all", "elements": [
            {**_btn(f"✅ Approve all {n['waiting']} waiting", "aidl_crv_approve_all", month, "primary"),
             "confirm": {"title": _t("Approve all cards?"), "confirm": _t("Approve all"), "deny": _t("Cancel"),
                         "text": _md(f"All {n['waiting']} waiting cards will be sent to your team on schedule.")}}]})

    cards = ca.month_cards(org, month)
    waiting_here = sum(c["approval"] == ca.WAITING for c in cards)
    nav = []
    if month > 1:
        nav.append(_btn(f"← Month {month - 1}", "aidl_crv_month", month - 1))
    if month < total_months:
        nav.append(_btn(f"Month {month + 1} →", "aidl_crv_month", month + 1))
    if waiting_here:
        nav.append(_btn(f"✅ Approve month {month}", "aidl_crv_approve_month", month, "primary"))
    blocks.append({"type": "section", "text": _md(f"*Month {month} of {total_months}*  ·  cards "
                                                  f"{cards[0]['position'] if cards else 0}–"
                                                  f"{cards[-1]['position'] if cards else 0}")})
    if nav:
        blocks.append({"type": "actions", "block_id": f"crv_nav_{month}", "elements": nav})
    for c in cards:
        swapped = f"  ·  🔄 replaces #{c['swapped_from']}" if c["swapped_from"] else ""
        options = [_opt("👁 Preview", f"preview|{c['no']}")]
        if c["approval"] != ca.APPROVED:
            options.append(_opt("✅ Approve", f"approve|{c['no']}"))
        if c["approval"] != ca.SKIPPED:
            options.append(_opt("⏭ Skip (never send)", f"skip|{c['no']}"))
        else:
            options.append(_opt("↩️ Undo skip", f"unskip|{c['no']}"))
        options.append(_opt("🔄 Swap for another card", f"swap|{c['no']}"))
        blocks.append({"type": "section",
                       "text": _md(f"{ICON[c['approval']]}  *{c['position']}. {c['title']}*\n"
                                   f"{c['category']} · {c['type']}{swapped}"),
                       "accessory": {"type": "overflow", "action_id": "aidl_crv_card", "options": options}})

    blocks += [
        {"type": "divider"},
        {"type": "section", "text": _md("*Extra reference cards (optional)* — 10 quick-reference cards you can send "
                                        "into the channel yourself."),
         "accessory": _btn("📬 Extra cards", "aidl_open_cards", "cards")},
    ]
    return {"type": "modal", "callback_id": CALLBACK, "title": _t("Approve cards"), "close": _t("Done"),
            "private_metadata": json.dumps({"month": month}), "blocks": blocks[:100]}


def preview_view(org, what: str) -> dict:
    """what = "onb|<item>" or "<card no>"."""
    from . import card_image
    from .aup_store import POINTS, current

    blocks = []
    key = ""
    if what.startswith("onb|"):
        item = what[4:]
        label = ca.ONBOARDING_LABEL.get(item, item)
        if item == "aup":
            aup = current(org)
            if aup is None:
                blocks.append({"type": "section", "text": _md("No AUP yet — create it in step 1.")})
            else:
                blocks.append({"type": "context", "elements": [_md(f"Employees accept the {POINTS} points one at a "
                                                                   "time, each as a card like these.")]})
                for n in range(1, len(aup["points"]) + 1):
                    kind = f"aup-{n}"
                    blocks.append({"type": "image", "image_url": card_image.image_url(kind, org),
                                   "alt_text": card_image.alt_text(kind, org), "title": _t(f"Point {n}")})
        else:
            blocks.append({"type": "image", "image_url": card_image.image_url(item, org),
                           "alt_text": card_image.alt_text(item)})
        key = f"onb|{item}"
        approved = ca.is_approved(org, item)
        title = label
    else:
        from .plans import card_by_no

        card = card_by_no(what) or {}
        kind = f"pkg-{what}"
        blocks.append({"type": "context", "elements": [_md(f"{card.get('category', '')} · {card.get('type', '')}")]})
        blocks.append({"type": "image", "image_url": card_image.image_url(kind, org),
                       "alt_text": card_image.alt_text(kind)})
        key = what
        approved = ca.status(org, f"pkg:{what}") == ca.APPROVED
        title = card.get("title", "Card")
    blocks.append({"type": "actions", "block_id": "crv_preview", "elements": [
        _btn("✅ Approved" if approved else "✅ Approve", "aidl_crv_preview_approve", key,
             "" if approved else "primary")]})
    return {"type": "modal", "callback_id": PREVIEW, "title": _t(title[:24]), "close": _t("Back"),
            "blocks": blocks[:100]}


def reminder_blocks(org, waiting_soon: list[dict], runs_out: str) -> list:
    """Weekly / urgent nudge to the admins who approve cards."""
    lines = "\n".join(f"⏳ *{c['position']}. {c['title']}* · {c['category']}" for c in waiting_soon[:8])
    more = f"\n…and {len(waiting_soon) - 8} more" if len(waiting_soon) > 8 else ""
    return [
        {"type": "section", "text": _md(f"🗂️ *Cards waiting for your approval* — {org.name}\n"
                                        f"{runs_out}\n\n{lines}{more}")},
        {"type": "actions", "block_id": "crv_remind", "elements": [
            _btn("📋 Review cards", "aidl_cards_review", "1", "primary"),
            {**_btn(f"✅ Approve these {len(waiting_soon)}", "aidl_crv_approve_list",
                    ",".join(c["no"] for c in waiting_soon)),
             "confirm": {"title": _t("Approve these cards?"), "confirm": _t("Approve"), "deny": _t("Cancel"),
                         "text": _md("They'll be sent to your team on schedule.")}},
        ]},
    ]
