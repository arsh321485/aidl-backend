"""✅ Got it and acknowledgement numbers for admins
(design: E:\\slack-cards.html, approved by the manager).

Employees
- Every awareness card has ✅ Got it · ☰ Menu (employee_flow.menu_view).
  Got it edits the same message to "✅ Acknowledged · <date>" and sets
  PackageDelivery.acked_at.
- A card not acknowledged after 7 days gets one reminder, as a reply in the
  card's thread (send_reminders, from the daily schedule_package_cards job).

Admins (no message per Got it)
- Admin Center: most / least acknowledged card (step 4); cards acknowledged
  this month, last week, today, and people overdue (reminded 3+ days ago,
  still not acknowledged) with ⏰ Remind (step 6).
- Team progress and Card approval show acknowledgement rates.
- 🔔 Acknowledgement alerts: per admin, what AIDL sends on top of that
  (setup_state["ack_alerts"]).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from django.utils import timezone

from . import slack_client
from .models import AIDLUser, Organization, PackageDelivery

logger = logging.getLogger(__name__)
REMIND_AFTER = timedelta(days=7)
OVERDUE_AFTER = timedelta(days=3)
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
LIVE = {"none": ("Don't message me", "Only the numbers in Admin Center"),
        "counter": ("One live message a day", "It updates itself, so at most one ping a day"),
        "every": ("A message for every Got it", "One DM each time someone taps Got it")}
LATE = {"alert": ("Overdue alert", "At most one a day, 3 days after their reminder"),
        "weekly": ("Only in the Admin Center", "")}


def _t(text: str) -> dict:
    return {"type": "plain_text", "text": text[:150], "emoji": True}


def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text[:2990]}


def _btn(text, action_id, value, style=""):
    b = {"type": "button", "text": _t(text), "action_id": action_id, "value": str(value)[:2000]}
    if style:
        b["style"] = style
    return b


def _short(d) -> str:
    return f"{d.day} {d.strftime('%b')}" if d else ""


def _bar(a: int, t: int) -> str:
    f = round(a / t * 10) if t else 0
    return "`" + "▰" * f + "▱" * (10 - f) + "`"


def _pct(a: int, t: int) -> int:
    return round(a / t * 100) if t else 0


def _slack_id(member: AIDLUser) -> str:
    return member.microsoft_id.split(":")[-1] if member.microsoft_id.startswith("slack:") else ""


def _card(no: str) -> dict:
    from .plans import card_by_no

    return card_by_no(no) or {"no": no, "title": f"Card {no}", "category": "", "type": "", "hook": "", "message": "",
                              "action": ""}


def _total(org: Organization) -> int:
    from .card_approval import sequence

    return len(sequence(org)) or 1


def received(member: AIDLUser) -> list[PackageDelivery]:
    """The cards Slack has posted to this learner, oldest first."""
    from .package_delivery import _mark_sent

    _mark_sent(member)
    return list(PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SENT)
                .order_by("post_at"))


def _message(org: Organization, row: PackageDelivery) -> dict:
    from .package_delivery import card_blocks

    card = _card(row.card_no)
    return {"text": f"AIDL card: {card['title']}",
            "blocks": card_blocks(org, card, row.post_order, _total(org), acked_at=row.acked_at)}


def find_message_ts(org: Organization, row: PackageDelivery) -> str:
    """The posted card's ts. Cards Slack posted from a schedule don't tell us
    their ts, so look it up in the DM (needs the im:history scope)."""
    if row.message_ts or not row.slack_channel_id:
        return row.message_ts
    oldest = row.post_at - timedelta(minutes=5)
    data = slack_client.slack_api("conversations.history", slack_client.bot_token(org), params={
        "channel": row.slack_channel_id, "oldest": f"{oldest.timestamp():.6f}",
        "latest": f"{(row.post_at + timedelta(hours=2)).timestamp():.6f}", "limit": 30})
    title = _card(row.card_no)["title"]
    for msg in data.get("messages") or []:
        if msg.get("text") == f"AIDL card: {title}":
            row.message_ts = msg.get("ts", "")
            row.save(update_fields=["message_ts"])
            return row.message_ts
    return ""


# ---------- ✅ Got it ----------

def acknowledge(org: Organization, member: AIDLUser, no: str, channel: str = "", ts: str = "",
                from_reminder: bool = False) -> PackageDelivery | None:
    row = PackageDelivery.objects.filter(user_id=str(member.pk), card_no=no).first()
    if row is None:
        return None
    if ts and not from_reminder and not row.message_ts:
        row.message_ts = ts
    first = row.acked_at is None
    if first:
        row.acked_at = timezone.now()
    row.save(update_fields=["acked_at", "message_ts"])
    token = slack_client.bot_token(org)
    msg_ts = find_message_ts(org, row)
    if msg_ts:  # the card itself → "✅ Acknowledged"
        slack_client.slack_api("chat.update", token, json={"channel": channel or row.slack_channel_id, "ts": msg_ts,
                                                           **_message(org, row)})
    if row.reminder_ts:
        slack_client.slack_api("chat.update", token, json={
            "channel": row.slack_channel_id or channel, "ts": row.reminder_ts, **reminder_message(row, member)})
    if first:
        after_ack(org, member, row)
    return row


def after_ack(org: Organization, member: AIDLUser, row: PackageDelivery) -> None:
    """Tell the admins who asked for it, and refresh the Admin Center numbers."""
    from .slack_blocks import publish_admin_center
    from .slack_onboarding import primary_admin

    title = _card(row.card_no)["title"]
    for admin, prefs in admin_prefs(org):
        if prefs["live"] == "every":
            _dm(org, admin, f"✅ *{member.full_name or member.email}* acknowledged *{title}*")
        elif prefs["live"] == "counter":
            _update_counter(org, admin)
    try:
        publish_admin_center(org, primary_admin(org))
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin center refresh after ack: %s", exc)


# ---------- reminders ----------

def reminder_message(row: PackageDelivery, member: AIDLUser) -> dict:
    title = _card(row.card_no)["title"]
    first = (member.full_name or "there").split()[0]
    blocks = [{"type": "context", "elements": [_md(f"💬 Reminder · *{title}*")]}]
    if row.reminded_by:
        blocks.append({"type": "section", "text": _md(f"👋 *{row.reminded_by}* (your AIDL admin) asked you to take a "
                                                      f"look at *{title}*. It takes about 30 seconds.")})
    else:
        blocks.append({"type": "section", "text": _md(f"👋 Quick one, {first}. Have you read *{title}*? "
                                                      "Tap Got it when you're done.")})
    if row.acked_at:
        blocks.append({"type": "context", "elements": [_md(f"✅ *Acknowledged* · {_short(row.acked_at)}")]})
    else:
        blocks.append({"type": "actions", "block_id": "aidl_nudge",
                       "elements": [_btn("✅ Got it", "aidl_card_ack", row.card_no, "primary")]})
    if not row.reminded_by:
        blocks.append({"type": "context", "elements": [_md("This is the only reminder for this card.")]})
    return {"text": f"Reminder: {title}", "blocks": blocks}


def _post_reminder(org: Organization, member: AIDLUser, row: PackageDelivery) -> bool:
    body = {"channel": row.slack_channel_id or _slack_id(member), **reminder_message(row, member)}
    ts = find_message_ts(org, row)
    if ts:
        body["thread_ts"] = ts  # a reply under the card
    res = slack_client.slack_api("chat.postMessage", slack_client.bot_token(org), json=body)
    if res.get("ok"):
        row.reminder_ts = res.get("ts", "")
        row.save(update_fields=["reminder_ts", "reminded_at", "reminded_by"])
    return bool(res.get("ok"))


def _active_rows(org: Organization):
    from .licensing import learners

    ids = [str(pk) for pk in learners(org).filter(licence_issued=True, slack_left_at__isnull=True)
           .values_list("pk", flat=True)]
    return PackageDelivery.objects.filter(organization_id=str(org.pk), user_id__in=ids,
                                          status=PackageDelivery.Status.SENT)


def send_reminders(org: Organization, now=None) -> int:
    """Daily: one reminder for each card still not acknowledged 7 days after
    it was sent. Returns how many were sent."""
    from .licensing import learners  # noqa: F401  (keeps the import order stable for tests)

    now = now or timezone.now()
    sent = 0
    for row in _active_rows(org).filter(acked_at__isnull=True, reminded_at__isnull=True, post_at__lte=now - REMIND_AFTER):
        member = AIDLUser.objects.filter(pk=row.user_id).first()
        if member is None:
            continue
        row.reminded_at = now
        sent += _post_reminder(org, member, row)
    return sent


def remind_person(org: Organization, admin: AIDLUser, user_id: str) -> bool:
    """⏰ Remind in the Admin Center: the admin asks again about the person's
    oldest overdue card."""
    member = AIDLUser.objects.filter(pk=user_id, organization_id=str(org.pk)).first()
    row = next((r for r in overdue_rows(org) if r.user_id == user_id), None)
    if member is None or row is None:
        return False
    row.reminded_by = admin.full_name or admin.email
    ok = _post_reminder(org, member, row)
    from .admin_setup import state, update

    reminded = dict(state(org).get("ack_reminded") or {})
    reminded[user_id] = {"by": str(admin.pk), "at": timezone.now().isoformat()}
    update(org, ack_reminded=reminded)
    return ok


def overdue_rows(org: Organization, now=None) -> list[PackageDelivery]:
    """Reminded 3+ days ago and still not acknowledged — oldest first."""
    now = now or timezone.now()
    return list(_active_rows(org).filter(acked_at__isnull=True, reminded_at__lte=now - OVERDUE_AFTER)
                .order_by("post_at"))


# ---------- numbers for admins ----------

def admin_stats(org: Organization, now=None) -> dict:
    from .admin_setup import state

    now = now or timezone.now()
    rows = list(_active_rows(org))
    names = {str(u.pk): (u.full_name or u.email) for u in AIDLUser.objects.filter(
        pk__in={r.user_id for r in rows})}
    month = [r for r in rows if r.post_at.year == now.year and r.post_at.month == now.month]
    week_start = (now - timedelta(days=now.weekday() + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    week = [r for r in rows if week_start <= r.post_at < week_start + timedelta(days=7)]
    today = sorted((r for r in rows if r.acked_at and r.acked_at.date() == now.date()),
                   key=lambda r: r.acked_at, reverse=True)
    rated = {}
    for r in rows:
        if r.post_at <= now - REMIND_AFTER:  # give people a week before judging a card
            a, t = rated.get(r.card_no, (0, 0))
            rated[r.card_no] = (a + bool(r.acked_at), t + 1)
    best = worst = None
    if rated:
        ordered = sorted(rated.items(), key=lambda kv: (kv[1][0] / kv[1][1], kv[1][1]))
        worst, best = ordered[0], ordered[-1]
    topics = {}
    for no, (a, t) in rated.items():
        cat = _card(no)["category"]
        ta, tt = topics.get(cat, (0, 0))
        topics[cat] = (ta + a, tt + t)
    topic_rates = sorted(((c, _pct(a, t)) for c, (a, t) in topics.items()), key=lambda x: x[1])
    reminded = state(org).get("ack_reminded") or {}
    late, seen = [], set()
    for r in overdue_rows(org, now):
        if r.user_id in seen:
            continue
        seen.add(r.user_id)
        note = reminded.get(r.user_id) or {}
        late.append({"user_id": r.user_id, "name": names.get(r.user_id, "Someone"), "title": _card(r.card_no)["title"],
                     "sent": r.post_at, "reminded_by_admin": note.get("at", "")})
    return {
        "sent": len(rows), "acked": sum(1 for r in rows if r.acked_at),
        "month_sent": len(month), "month_acked": sum(1 for r in month if r.acked_at),
        "week_start": week_start, "week_sent": len(week), "week_acked": sum(1 for r in week if r.acked_at),
        "today": [(names.get(r.user_id, "Someone"), _card(r.card_no)["title"], r.acked_at) for r in today],
        "best": (_card(best[0])["title"], _pct(*best[1])) if best else None,
        "worst": (_card(worst[0])["title"], _pct(*worst[1])) if worst and worst != best else None,
        "topics_top": topic_rates[::-1][:2], "topics_low": topic_rates[:2],
        "card_rates": {no: _pct(a, t) for no, (a, t) in rated.items()},
        "recent": sorted({r.card_no for r in rows if r.post_at > now - REMIND_AFTER}),
        "overdue": late,
    }


def member_stats(org: Organization) -> dict:
    """{user_id: (acknowledged, received, overdue)}"""
    out = {}
    late = {}
    for r in overdue_rows(org):
        late[r.user_id] = late.get(r.user_id, 0) + 1
    for r in _active_rows(org):
        a, t, _o = out.get(r.user_id, (0, 0, 0))
        out[r.user_id] = (a + bool(r.acked_at), t + 1, late.get(r.user_id, 0))
    return out


def today_line(s: dict) -> str:
    items = s["today"]
    if not items:
        return "✅ *Today: nobody has acknowledged a card yet*"
    shown = "  ·  ".join(f"{n} · {t} _{a.strftime('%H:%M')}_" for n, t, a in items[:4])
    more = f"  ·  +{len(items) - 4} more" if len(items) > 4 else ""
    return f"✅ *Today: {len(items)} acknowledged*  ·  {shown}{more}"


# ---------- 🔔 acknowledgement alerts (per admin) ----------

def _defaults(org: Organization, admin: AIDLUser) -> dict:
    from .admin_perms import is_master

    return {"live": "counter", "late": "alert"} if is_master(org, admin) else {"live": "none", "late": "weekly"}


def prefs_for(org: Organization, admin: AIDLUser) -> dict:
    from .admin_setup import state

    saved = (state(org).get("ack_alerts") or {}).get(str(admin.pk))
    return {**_defaults(org, admin), **(saved or {})}


def admin_prefs(org: Organization):
    for admin in AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True,
                                         microsoft_id__startswith="slack:"):
        yield admin, prefs_for(org, admin)


def alerts_view(org: Organization, admin: AIDLUser, meta: dict) -> dict:
    p = prefs_for(org, admin)

    def radios(options: dict, current: str) -> dict:
        opts = []
        for value, (label, desc) in options.items():
            o = {"text": _t(label), "value": value}
            if desc:
                o["description"] = _t(desc)
            opts.append(o)
        return {"type": "radio_buttons", "action_id": "v", "options": opts,
                "initial_option": next(o for o in opts if o["value"] == current)}

    return {"type": "modal", "callback_id": "aidl_ack_alerts", "title": _t("Acknowledgement alerts"),
            "submit": _t("Save"), "close": _t("Cancel"), "private_metadata": json.dumps(meta), "blocks": [
        {"type": "section", "text": _md("You always see acknowledgements in the Admin Center and Team progress. "
                                        "Choose what AIDL *sends* you on top of that.")},
        {"type": "input", "block_id": "live", "label": _t("When someone taps ✅ Got it"),
         "element": radios(LIVE, p["live"])},
        {"type": "input", "block_id": "late", "label": _t("When someone doesn't acknowledge"),
         "element": radios(LATE, p["late"])},
        {"type": "context", "elements": [_md("Today, last week and overdue people are always in step 6 of your "
                                             "Admin Center.")]},
    ]}


def save_alerts(org: Organization, admin: AIDLUser, view: dict) -> dict:
    from .admin_setup import state, update

    values = (view.get("state") or {}).get("values") or {}
    pick = lambda b: ((values.get(b) or {}).get("v") or {}).get("selected_option", {}).get("value")  # noqa: E731
    live, late = pick("live"), pick("late")
    if live not in LIVE or late not in LATE:
        return {"response_action": "errors", "errors": {"live": "Choose an option."}}
    saved = dict(state(org).get("ack_alerts") or {})
    saved[str(admin.pk)] = {"live": live, "late": late}
    update(org, ack_alerts=saved)
    return {"notice": f"🔔 Saved. Got it updates: *{LIVE[live][0]}* · overdue: *{LATE[late][0]}*."}


def _dm(org: Organization, admin: AIDLUser, text: str, blocks: list | None = None) -> dict:
    return slack_client.send_dm(slack_client.bot_token(org), org, _slack_id(admin), text=text,
                                blocks=blocks or [{"type": "section", "text": _md(text)}])


def _update_counter(org: Organization, admin: AIDLUser) -> None:
    """"One live message a day": today's acknowledgements, edited in place."""
    from .admin_setup import state, update

    s = admin_stats(org)
    text = today_line(s)
    day = timezone.now().date().isoformat()
    counters = dict(state(org).get("ack_counter") or {})
    mine = counters.get(str(admin.pk)) or {}
    blocks = [{"type": "section", "text": _md(text)},
              {"type": "context", "elements": [_md("This message updates itself through the day.")]}]
    if mine.get("day") == day and mine.get("ts"):
        res = slack_client.slack_api("chat.update", slack_client.bot_token(org), json={
            "channel": mine["channel"], "ts": mine["ts"], "text": text, "blocks": blocks})
        if res.get("ok"):
            return
    res = _dm(org, admin, text, blocks)
    if res.get("ok"):
        counters[str(admin.pk)] = {"day": day, "channel": res.get("channel", ""), "ts": res.get("ts", "")}
        update(org, ack_counter=counters)


def send_overdue_alerts(org: Organization) -> int:
    """Daily: admins who chose "Overdue alert" get one DM listing them."""
    late = admin_stats(org)["overdue"]
    if not late:
        return 0
    told = 0
    for admin, prefs in admin_prefs(org):
        if prefs["late"] != "alert":
            continue
        blocks = [{"type": "section", "text": _md(f"⏰ *{len(late)} {'person is' if len(late) == 1 else 'people are'} "
                                                  "overdue* · reminded 3+ days ago, still not acknowledged")}]
        for p in late[:10]:
            blocks.append({"type": "section", "text": _md(f"*{p['name']}* · {p['title']} (sent {_short(p['sent'])})"),
                           "accessory": _btn("⏰ Remind", "aidl_ack_remind", p["user_id"])})
        told += bool(_dm(org, admin, "AIDL: people overdue on their cards", blocks).get("ok"))
    return told


# ---------- the list on the Teams My cards page ----------

def _blank_state(**extra) -> dict:
    return {"q": "", "status": "all", "topic": "all", "month": "all", "latest": "all", "view": "card", "idx": 0,
            "page": 0, **extra}


def _filtered(rows: list[PackageDelivery], m: dict) -> list[PackageDelivery]:
    lst = list(reversed(rows))  # newest first
    if m["latest"] != "all":
        lst = lst[: int(m["latest"])]
    if m["status"] == "todo":
        lst = [r for r in lst if not r.acked_at]
    elif m["status"] == "done":
        lst = [r for r in lst if r.acked_at]
    if m["month"] != "all":
        lst = [r for r in lst if r.post_at.strftime("%Y-%m") == m["month"]]
    if m["topic"] != "all":
        lst = [r for r in lst if _card(r.card_no)["category"] == m["topic"]]
    q = m["q"].strip().lower()
    if q:
        lst = [r for r in lst if q in " ".join(str(_card(r.card_no).get(k, "")) for k in
                                                ("title", "category", "type", "hook", "message", "action")).lower()]
    return lst


def _month_name(key: str) -> str:
    return f"{MONTHS[int(key[5:]) - 1]} {key[:4]}"


def card_title(no: str) -> str:
    return _card(no)["title"]


def parse_time(value: str):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
