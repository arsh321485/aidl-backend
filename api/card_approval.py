"""Admins approve every card before the team gets it.

Two kinds of card:
- Onboarding: the Traffic Light Check, the Highway Code and the AUP (each
  AUP version is approved on its own: "aup:<version>"). The programme can't
  start until all three are approved.
- Awareness: the plan's package (plans.package_cards), "pkg:<card no>".
  Admins approve, skip (never sent) or swap (replaced by the next unused
  card from the same topic, which then needs approving) each card, a month
  or everything at once.

Nothing is sent without approval. A card that isn't approved by its slot is
postponed: the slot goes to the next approved card, and the waiting card
follows once it's approved (package_delivery.sync_package).

Decisions are CardApproval rows (no row = waiting) — who decided and when is
the audit trail.
"""

from __future__ import annotations

import math

from django.utils import timezone

from .models import AIDLUser, CardApproval, Organization

ONBOARDING = ("traffic_light", "highway_code", "aup")
ONBOARDING_LABEL = {"traffic_light": "🚦 Traffic Light Check", "highway_code": "📖 The Highway Code",
                    "aup": "📄 AI Acceptable Use Policy"}
APPROVED, SKIPPED, WAITING = "approved", "skipped", "waiting"


def _rows(org: Organization) -> dict[str, CardApproval]:
    return {r.card_key: r for r in CardApproval.objects.filter(organization_id=str(org.pk))}


def onboarding_key(org: Organization, item: str) -> str:
    if item == "aup":
        from .aup_store import version

        ver = version(org)
        return f"aup:{ver}" if ver else ""
    return item


def status(org: Organization, key: str, rows: dict | None = None) -> str:
    if not key:
        return WAITING
    row = (rows if rows is not None else _rows(org)).get(key)
    return row.status if row is not None else WAITING


def is_approved(org: Organization, item: str) -> bool:
    """Onboarding item ("traffic_light" / "highway_code" / "aup")."""
    return status(org, onboarding_key(org, item)) == APPROVED


def onboarding_ready(org: Organization) -> bool:
    rows = _rows(org)
    return all(status(org, onboarding_key(org, i), rows) == APPROVED for i in ONBOARDING)


def onboarding(org: Organization) -> list[dict]:
    rows = _rows(org)
    out = []
    for item in ONBOARDING:
        key = onboarding_key(org, item)
        row = rows.get(key) if key else None
        out.append({"item": item, "key": key, "label": ONBOARDING_LABEL[item], "exists": bool(key),
                    "status": row.status if row else WAITING, "by": row.decided_by_name if row else "",
                    "at": row.decided_at if row else None})
    return out


# ---------- awareness cards ----------

def sequence(org: Organization, rows: dict | None = None) -> list[dict]:
    """The package in posting order with swaps applied; each card gets
    "position" and "approval" (approved / skipped / waiting) and, if it
    replaced another, "swapped_from"."""
    from .plans import card_by_no, package_cards

    rows = _rows(org) if rows is None else rows
    out = []
    for position, card in enumerate(package_cards(org), 1):
        seen = set()
        swapped_from = ""
        row = rows.get(f"pkg:{card['no']}")
        # follow swaps: A skipped → B (B may itself be swapped → C …)
        while row is not None and row.status == SKIPPED and row.replacement_no and row.replacement_no not in seen:
            seen.add(card["no"])
            swapped_from = swapped_from or card["no"]
            nxt = card_by_no(row.replacement_no)
            if nxt is None:
                break
            card = nxt
            row = rows.get(f"pkg:{card['no']}")
        out.append({**card, "position": position, "approval": row.status if row else WAITING,
                    "swapped_from": swapped_from, "by": row.decided_by_name if row else ""})
    return out


def deliverable(org: Organization) -> list[dict]:
    """Approved cards, in posting order."""
    return [c for c in sequence(org) if c["approval"] == APPROVED]


def counts(org: Organization) -> dict:
    seq = sequence(org)
    return {"total": len(seq), "approved": sum(c["approval"] == APPROVED for c in seq),
            "skipped": sum(c["approval"] == SKIPPED for c in seq),
            "waiting": sum(c["approval"] == WAITING for c in seq)}


def per_month(org: Organization) -> int:
    from .plans import package_cards

    return max(1, math.ceil(len(package_cards(org)) / 12))


def month_of(org: Organization, position: int) -> int:
    """Month 1… of the programme a card falls in (by posting order)."""
    return (position - 1) // per_month(org) + 1


def months(org: Organization) -> int:
    from .plans import package_cards

    return max(1, month_of(org, len(package_cards(org))))


def month_cards(org: Organization, month: int) -> list[dict]:
    return [c for c in sequence(org) if month_of(org, c["position"]) == month]


# ---------- decisions ----------

def _decide(org: Organization, key: str, decision: str, by: AIDLUser | None, replacement: str = "") -> None:
    CardApproval.objects.update_or_create(
        organization_id=str(org.pk), card_key=key,
        defaults={"status": decision, "replacement_no": replacement,
                  "decided_by_id": str(by.pk) if by else "", "decided_by_name": (by.full_name or by.email) if by else "",
                  "decided_at": timezone.now()})


def approve_onboarding(org: Organization, item: str, by: AIDLUser | None) -> bool:
    key = onboarding_key(org, item)
    if not key:
        return False
    _decide(org, key, APPROVED, by)
    return True


def approve(org: Organization, nos: list[str], by: AIDLUser | None) -> int:
    """Approve these awareness cards (a skipped card is approved again; a
    swapped-out one stays replaced). Returns how many changed."""
    rows = _rows(org)
    changed = 0
    for no in nos:
        row = rows.get(f"pkg:{no}")
        if row is not None and (row.status == APPROVED or row.replacement_no):
            continue
        _decide(org, f"pkg:{no}", APPROVED, by)
        changed += 1
    return changed


def approve_waiting(org: Organization, by: AIDLUser | None, month: int | None = None) -> int:
    cards = month_cards(org, month) if month else sequence(org)
    return approve(org, [c["no"] for c in cards if c["approval"] == WAITING], by)


def skip(org: Organization, no: str, by: AIDLUser | None) -> None:
    _decide(org, f"pkg:{no}", SKIPPED, by)
    cancel_card(org, no)


def unskip(org: Organization, no: str, by: AIDLUser | None) -> None:
    """Undo a skip (not a swap): the card waits for approval again."""
    CardApproval.objects.filter(organization_id=str(org.pk), card_key=f"pkg:{no}", replacement_no="").delete()


def swap(org: Organization, no: str, by: AIDLUser | None) -> dict | None:
    """Replace a card with the next unused one from the same topic. Returns
    the new card (waiting for approval), or None if the topic has none left."""
    from .plans import card_by_no, library

    card = card_by_no(no)
    if card is None:
        return None
    rows = _rows(org)
    used = {c["no"] for c in sequence(org, rows)} | {k[4:] for k in rows if k.startswith("pkg:")} | {no}
    code = card.get("code") or next((c["code"] for c in library() if c["no"] == no), "")
    new = next((c for c in library() if c.get("code") == code and c["no"] not in used), None)
    if new is None:
        return None
    _decide(org, f"pkg:{no}", SKIPPED, by, replacement=new["no"])
    cancel_card(org, no)
    return new


def cancel_card(org: Organization, no: str) -> None:
    """A skipped card that was already scheduled for someone is cancelled."""
    from . import slack_client
    from .models import PackageDelivery

    token = slack_client.bot_token(org)
    for row in PackageDelivery.objects.filter(organization_id=str(org.pk), card_no=no,
                                              status=PackageDelivery.Status.SCHEDULED):
        if row.scheduled_message_id and token:
            slack_client.slack_api("chat.deleteScheduledMessage", token, json={
                "channel": row.slack_channel_id, "scheduled_message_id": row.scheduled_message_id})
        row.delete()


def after_change(org: Organization, by: AIDLUser | None, summary: str) -> None:
    """After approvals changed: schedule the newly approved cards for every
    licensed learner, refresh the Admin Center and tell the master admin."""
    from .admin_perms import notify_master
    from .package_delivery import sync_organization

    sync_organization(org, full=False)
    try:
        from .slack_blocks import publish_admin_center

        publish_admin_center(org, by)
    except Exception:  # noqa: BLE001 — the decision itself is saved
        pass
    if by is not None and summary:
        notify_master(org, by, f"🗂️ *{by.full_name or by.email}* {summary}")


# ---------- reminders (python manage.py schedule_package_cards, daily) ----------

def runs_out(org: Organization):
    """When the first licensed learner runs out of approved cards (their
    last scheduled card), or None."""
    from django.db.models import Max

    from .licensing import learners
    from .models import PackageDelivery

    ids = [str(pk) for pk in learners(org).filter(licence_issued=True, slack_left_at__isnull=True)
           .values_list("pk", flat=True)]
    if not ids:
        return None
    last = {}
    for row in (PackageDelivery.objects.filter(user_id__in=ids, status=PackageDelivery.Status.SCHEDULED)
                .values("user_id").annotate(last=Max("post_at"))):
        last[row["user_id"]] = row["last"]
    return min(last.get(i) or timezone.now() for i in ids)


def remind(org: Organization, now=None) -> int:
    """DM the admins who approve cards when cards are waiting: once a week,
    or after 2 days when someone runs out of approved cards within 3 days.
    Returns how many admins were told."""
    from datetime import datetime, timedelta

    from . import slack_client
    from .admin_perms import has
    from .admin_setup import state, update
    from .card_review_slack import reminder_blocks

    now = now or timezone.now()
    waiting = [c for c in sequence(org) if c["approval"] == WAITING][:20]
    if not waiting or not org.slack_bot_token:
        return 0
    last = state(org).get("approval_reminded_at")
    last_at = datetime.fromisoformat(last) if last else None
    out = runs_out(org)
    urgent = out is not None and out <= now + timedelta(days=3)
    due = last_at is None or now - last_at >= timedelta(days=7) or (urgent and now - last_at >= timedelta(days=2))
    if not due:
        return 0
    if out is None:
        when = "Approve them so they're ready when your team gets their licences."
    elif out <= now:
        when = "⚠️ *Your team has no approved cards left* — their cards are postponed until you approve more."
    else:
        when = f"{'⚠️ ' if urgent else ''}Your team runs out of approved cards on *{out.strftime('%d %B')}*."
    token = slack_client.bot_token(org)
    told = 0
    for admin in AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True,
                                         microsoft_id__startswith="slack:"):
        if has(org, admin, "approve_cards"):
            res = slack_client.send_dm(token, org, admin.microsoft_id.split(":")[-1],
                                       text=f"{len(waiting)} AIDL cards are waiting for your approval",
                                       blocks=reminder_blocks(org, waiting, when))
            told += 1 if res.get("ok") or res.get("fallback_ok") else 0
    update(org, approval_reminded_at=now.isoformat())
    return told
