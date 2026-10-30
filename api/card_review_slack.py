"""Slack popup: Admin Center step 4 "Review & approve cards" (card_approval.py).

    Onboarding cards   🚦 Traffic Light · 📖 Highway Code  [👁 Preview] [✅ Approve]
    From your AI policy  5 ready-made awareness cards  [👁 Preview] [✅ Approve]
    Awareness cards    counts · [✅ Approve all waiting]
      Month n of N     [← Month n-1] [Month n+1 →] [✅ Approve month n] [👤 Ask an admin]
        one line per card with a ⋯ menu: Preview · Approve · Skip · Swap · Ask an admin
    Your AI policy     the policy cards sent / scheduled · [📌 Add a policy card] [＋ Request a new card]

"Ask another admin to approve" (card_approval.assign) hands a card or a
month to another admin; they get a DM and see only those cards
(mine_view), which they can approve or skip without the full permission.
The paid extra reference cards are gone from this flow.

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


def review_view(org, month: int = 1, notice: str = "", *, can_request: bool = False) -> dict:
    from .plans import PACKAGE_LABELS, plan_key
    from .policy_cards import summary as policy_summary

    assignee = ca.assignee_names(org)

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
        who = f" by {row['by']}" if row["status"] == ca.APPROVED and row["by"] else ""
        blocks.append({"type": "section", "text": _md(f"{ICON[row['status']]}  *{row['label']}*  ·  "
                                                      f"{WORD[row['status']]}{who}")})
        blocks.append({"type": "actions", "block_id": f"crv_onb_{row['item']}", "elements": [
            _btn("👁 Preview", "aidl_crv_preview", f"onb|{row['item']}"),
            *([_btn("✅ Approve", "aidl_crv_approve_onb", row["item"], "primary")]
              if row["status"] != ca.APPROVED else []),
        ]})

    blocks += [{"type": "divider"}, {"type": "section", "text": _md(
        "*From your AI policy* — general rules as ready-made awareness cards. Approved ones follow each licence; "
        "employees press *Got it*.")}]
    for c in ca.policy_awareness(org):
        mine = assignee.get(f"paw:{c['id']}") if c["status"] != ca.APPROVED else ""
        blocks.append({"type": "section", "text": _md(f"{ICON[c['status']]}  {c['icon']} *{c['title']}*  ·  "
                                                      f"{WORD[c['status']]}" + (f"  ·  👤 {mine}" if mine else ""))})
        blocks.append({"type": "actions", "block_id": f"crv_paw_{c['id']}", "elements": [
            _btn("👁 Preview", "aidl_crv_preview", f"paw|{c['id']}"),
            *([_btn("✅ Approve", "aidl_crv_approve_paw", c["id"], "primary"),
               _btn("👤 Ask an admin", "aidl_crv_assign", f"paw:{c['id']}")] if c["status"] != ca.APPROVED else []),
        ]})

    n = ca.counts(org)
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": _md(f"*Awareness cards — {PACKAGE_LABELS[plan_key(org)]}* ({n['total']})\n"
                                        f"✅ {n['approved']} approved  ·  ⏳ {n['waiting']} waiting  ·  "
                                        f"⏭ {n['skipped']} skipped")},
    ]
    from .card_ack import admin_stats

    acks = admin_stats(org)
    if acks["sent"] and (acks["topics_top"] or acks["topics_low"]):
        top = " · ".join(f"{c} {p}%" for c, p in acks["topics_top"])
        low = " · ".join(f"{c} {p}%" for c, p in acks["topics_low"] if (c, p) not in acks["topics_top"])
        blocks.append({"type": "section", "text": _md(f"📘 *How topics are landing* · {acks['sent']} cards sent so far\n"
                                                      f"🏆 {top}" + (f"\n🔻 {low}" if low else ""))})
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
        nav.append(_btn("👤 Ask an admin", "aidl_crv_assign", f"month:{month}"))
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
        if c["approval"] == ca.WAITING:
            options.append(_opt("👤 Ask another admin", f"assign|{c['no']}"))
        mine = assignee.get(f"pkg:{c['no']}") if c["approval"] == ca.WAITING else ""
        blocks.append({"type": "section",
                       "text": _md(f"{ICON[c['approval']]}  *{c['position']}. {c['title']}*\n"
                                   f"{c['category']} · {c['type']}{swapped}" + (f"  ·  👤 {mine}" if mine else "")),
                       "accessory": {"type": "overflow", "action_id": "aidl_crv_card", "options": options}})
        if acks["sent"]:
            rate = acks["card_rates"].get(c["no"])
            if rate is not None:
                note = f"{rate}% acknowledged" + (" 🔻" if rate <= 50 else "")
            elif c["no"] in acks["recent"]:
                note = "sent this week · too early to tell"
            else:
                note = "not sent yet"
            blocks.append({"type": "context", "elements": [_md(f"📘 {note}")]})

    policy = policy_summary(org)
    sent = sum(c["state"] == "sent" for c in policy)
    later = len(policy) - sent
    line = (f"{sent} of 14 policy cards approved" + (f" · {later} scheduled" if later else "")) if policy else \
        "No policy cards yet — each topic of your AI policy is one short card your team agrees to."
    buttons = [_btn("📌 Add a policy card", "aidl_policy_open", "policy")]
    if can_request:
        buttons.append(_btn("＋ Request a new card", "aidl_card_new", "new"))
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": _md(f"*Your AI policy* — approved when you send each card.\n{line}")},
        {"type": "actions", "block_id": "crv_policy", "elements": buttons},
    ]
    if can_request:
        blocks.append({"type": "context", "elements": [_md("Need a card that isn't here? Request it — AIDL's team "
                                                           "reviews every request.")]})
    return {"type": "modal", "callback_id": CALLBACK, "title": _t("Approve cards"), "close": _t("Done"),
            "private_metadata": json.dumps({"month": month}), "blocks": blocks[:100]}


def mine_view(org, user, notice: str = "") -> dict:
    """The cards another admin asked `user` to approve."""
    from .plans import card_by_no
    from .policy_awareness import by_id

    rows = ca._rows(org)
    blocks = [{"type": "section", "text": _md("*Cards you've been asked to approve.* Nothing reaches your team "
                                              "until it's approved.")}]
    if notice:
        blocks.append({"type": "context", "elements": [_md(notice)]})
    keys = ca.assigned_keys(org, user)
    for key in keys:
        status = ca.status(org, key, rows)
        if key.startswith("pkg:"):
            card = card_by_no(key[4:]) or {"title": key[4:], "category": "", "type": ""}
            options = [_opt("👁 Preview", f"preview|{key[4:]}")]
            if status != ca.APPROVED:
                options.append(_opt("✅ Approve", f"approve|{key[4:]}"))
            if status != ca.SKIPPED:
                options.append(_opt("⏭ Skip (never send)", f"skip|{key[4:]}"))
            blocks.append({"type": "section", "text": _md(f"{ICON[status]}  *{card['title']}*\n"
                                                          f"{card.get('category', '')} · {card.get('type', '')}"),
                           "accessory": {"type": "overflow", "action_id": "aidl_crv_card", "options": options}})
        elif key.startswith("paw:"):
            card = by_id(key[4:]) or {"id": key[4:], "icon": "", "title": key[4:]}
            blocks.append({"type": "section", "text": _md(f"{ICON[status]}  {card['icon']} *{card['title']}*  ·  "
                                                          "from your AI policy")})
            blocks.append({"type": "actions", "block_id": f"crv_mine_{card['id']}", "elements": [
                _btn("👁 Preview", "aidl_crv_preview", f"paw|{card['id']}"),
                *([_btn("✅ Approve", "aidl_crv_approve_paw", card["id"], "primary")] if status != ca.APPROVED else [])]})
    if not keys:
        blocks.append({"type": "section", "text": _md("_No cards are waiting for you._")})
    return {"type": "modal", "callback_id": CALLBACK, "title": _t("Your cards to approve"), "close": _t("Done"),
            "private_metadata": json.dumps({"mine": True}), "blocks": blocks[:100]}


ASSIGN = "aidl_crv_assign"


def assign_view(org, user, what: str, meta: dict | None = None) -> dict:
    """Pick the admin who should approve `what` ("month:<n>", "<card no>"
    or "paw:<id>")."""
    from .models import AIDLUser

    admins = [a for a in AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN,
                                                 is_active=True).order_by("full_name") if a.pk != user.pk]
    if what.startswith("month:"):
        n = int(what[6:] or 1)
        label = f"the waiting cards of month {n} ({sum(c['approval'] == ca.WAITING for c in ca.month_cards(org, n))})"
    elif what.startswith("paw:"):
        from .policy_awareness import by_id

        label = f"*{(by_id(what[4:]) or {}).get('title', 'this card')}*"
    else:
        from .plans import card_by_no

        label = f"*{(card_by_no(what) or {}).get('title', 'this card')}*"
    if not admins:
        return {"type": "modal", "callback_id": PREVIEW, "title": _t("Ask an admin"), "close": _t("Back"),
                "blocks": [{"type": "section", "text": _md("There's no other admin yet. Add one in step 2 of the "
                                                           "Admin Center, then ask them here.")}]}
    options = [_opt(a.full_name or a.email, str(a.pk)) for a in admins[:100]]
    return {"type": "modal", "callback_id": ASSIGN, "title": _t("Ask an admin"), "submit": _t("Ask them"),
            "close": _t("Back"), "private_metadata": json.dumps({**(meta or {}), "what": what}), "blocks": [
                {"type": "section", "text": _md(f"Who should approve {label}? They get a message and can approve "
                                                "or skip just these cards — they don't need the *Approve cards* "
                                                "permission.")},
                {"type": "input", "block_id": "admin", "label": _t("Admin"),
                 "element": {"type": "static_select", "action_id": "v", "options": options,
                             "placeholder": _t("Choose an admin")}}]}


def assign_keys(org, what: str) -> list[str]:
    if what.startswith("month:"):
        return [f"pkg:{c['no']}" for c in ca.month_cards(org, int(what[6:] or 1)) if c["approval"] == ca.WAITING]
    if what.startswith("paw:"):
        return [what]
    return [f"pkg:{what}"]


def submit_assign(caller, org, view: dict) -> dict:
    """Save who approves these cards and tell them."""
    from . import slack_client
    from .admin_perms import notify_master
    from .models import AIDLUser

    meta = json.loads(view.get("private_metadata") or "{}")
    picked = (((view.get("state") or {}).get("values") or {}).get("admin") or {}).get("v") or {}
    admin_id = (picked.get("selected_option") or {}).get("value", "")
    to = AIDLUser.objects.filter(pk=admin_id, organization_id=str(org.pk), role=AIDLUser.Role.ADMIN).first()
    if to is None:
        return {"response_action": "errors", "errors": {"admin": "Choose an admin."}}
    keys = assign_keys(org, meta.get("what", ""))
    if not keys:
        return {"response_action": "errors", "errors": {"admin": "These cards don't need approving any more."}}
    ca.assign(org, keys, to, caller)
    who = caller.full_name or caller.email
    if to.microsoft_id.startswith("slack:"):
        slack_client.send_dm(slack_client.bot_token(org), org, to.microsoft_id.split(":")[-1],
                             text=f"{who} asked you to approve {len(keys)} AIDL card(s)", blocks=[
            {"type": "section", "text": _md(f"🗂️ *{who}* asked you to approve *{len(keys)}* card"
                                            f"{'s' if len(keys) != 1 else ''} for {org.name}'s team.")},
            {"type": "actions", "block_id": "crv_mine_dm", "elements": [
                _btn("📋 Review my cards", "aidl_cards_review", "mine", "primary")]}])
    notify_master(org, caller, f"👤 *{who}* asked *{to.full_name or to.email}* to approve {len(keys)} card(s).")
    return {"notice": f"👤 Asked {to.full_name or to.email} to approve {len(keys)} card(s)."}


def preview_view(org, what: str) -> dict:
    """what = "onb|<item>", "paw|<id>" or "<card no>"."""
    from . import card_image

    blocks = []
    key = ""
    if what.startswith("onb|"):
        item = what[4:]
        label = ca.ONBOARDING_LABEL.get(item, item)
        blocks.append({"type": "image", "image_url": card_image.image_url(item, org),
                       "alt_text": card_image.alt_text(item)})
        key = f"onb|{item}"
        approved = ca.is_approved(org, item)
        title = label
    elif what.startswith("paw|"):
        from .policy_awareness import by_id, key as paw_key

        card = by_id(what[4:]) or {"id": what[4:], "title": "Card", "lines": []}
        kind = f"paw-{card['id']}"
        blocks.append({"type": "context", "elements": [_md("From your AI policy · awareness card · employees press "
                                                           "*Got it*")]})
        blocks.append({"type": "image", "image_url": card_image.image_url(kind, org),
                       "alt_text": card_image.alt_text(kind, org)})
        key = what
        approved = ca.status(org, paw_key(card["id"])) == ca.APPROVED
        title = card["title"]
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
