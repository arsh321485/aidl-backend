"""Cards catalogue business logic — quota, request/send/schedule, and the
scheduled-send dispatch used by both the live POST endpoints and the
dispatch_scheduled_cards management command."""

from __future__ import annotations

import logging
from datetime import datetime

from django.utils import timezone

from .cards_catalog_data import CARD_BY_ID, CARD_CATALOG, MONTHLY_CARD_QUOTA
from .microsoft_auth import get_access_token_for_user
from .models import AIDLUser, CardCustomRequest, CardRequest, Organization


logger = logging.getLogger(__name__)


def _org_id(org: Organization) -> str:
    return str(org.pk)


def _month_start(now=None):
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def quota_for_org(org: Organization, *, now=None) -> dict:
    used = CardRequest.objects.filter(
        organization_id=_org_id(org),
        requested_at__gte=_month_start(now),
    ).count()
    return {"used": used, "max": MONTHLY_CARD_QUOTA}


def _latest_status_by_card(org: Organization) -> dict[str, CardRequest]:
    latest: dict[str, CardRequest] = {}
    rows = CardRequest.objects.filter(organization_id=_org_id(org)).order_by("-requested_at")
    for row in rows:
        latest.setdefault(row.card_id, row)
    return latest


def get_catalog_for_org(org: Organization) -> list[dict]:
    latest = _latest_status_by_card(org)
    catalog = []
    for card in CARD_CATALOG:
        row = latest.get(card["id"])
        catalog.append(
            {
                "id": card["id"],
                "icon": card["icon"],
                "title": card["title"],
                "category": card["category"],
                "desc": card["desc"],
                "price": card["price"],
                "rating": card["rating"],
                "status": row.status if row else "not_sent",
                "scheduled_at": row.scheduled_at.isoformat() if row and row.scheduled_at else "",
                "sent_at": row.sent_at.isoformat() if row and row.sent_at else "",
            }
        )
    return catalog


def get_cards_payload(org: Organization) -> dict:
    quota = quota_for_org(org)
    return {
        "catalog": get_catalog_for_org(org),
        "quota_used": quota["used"],
        "quota_max": quota["max"],
    }


class CardsError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(message or code)


def _require_card(card_id: str) -> dict:
    card = CARD_BY_ID.get(card_id)
    if card is None:
        raise CardsError("unknown_card", "No such reference card.")
    return card


def request_card(org: Organization, *, card_id: str, by_email: str) -> CardRequest:
    _require_card(card_id)
    quota = quota_for_org(org)
    if quota["used"] >= quota["max"]:
        raise CardsError("quota_exceeded", f"Monthly quota of {quota['max']} cards reached.")
    return CardRequest.objects.create(
        organization_id=_org_id(org),
        card_id=card_id,
        status=CardRequest.Status.REQUESTED,
        requested_by_email=by_email,
    )


def build_reference_card_message(card: dict) -> dict:
    """A simple Adaptive Card for one reference card, posted into the org's
    aidl dashboard channel via send_channel_adaptive_card."""
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"{card['icon']} {card['title']}",
                "size": "Large",
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": card["category"],
                "isSubtle": True,
                "size": "Small",
                "spacing": "None",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": card["body"],
                "wrap": True,
                "spacing": "Medium",
            },
        ],
        "msteams": {"width": "Full"},
    }


def send_card_now(org: Organization, *, card_id: str, by_user: AIDLUser) -> dict:
    """Posts the card into the org's aidl dashboard channel right now, using
    the sending admin's own stored Graph token."""
    from .teams_messaging import send_channel_adaptive_card

    card = _require_card(card_id)
    team_id = getattr(by_user, "teams_team_id", "") or ""
    channel_id = org.teams_welcome_channel_id or getattr(by_user, "teams_channel_id", "") or ""
    if not team_id or not channel_id:
        raise CardsError("channel_not_configured", "aidl dashboard channel is not linked yet.")

    access_token = get_access_token_for_user(by_user)
    if not access_token:
        raise CardsError("missing_refresh_token", "Re-login via Teams to refresh Graph access.")

    result = send_channel_adaptive_card(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=build_reference_card_message(card),
    )
    quota = quota_for_org(org)
    if not result.get("ok"):
        CardRequest.objects.create(
            organization_id=_org_id(org),
            card_id=card_id,
            status=CardRequest.Status.FAILED,
            requested_by_email=by_user.email,
            error=(result.get("detail") or result.get("error") or "")[:255],
        )
        raise CardsError("send_failed", result.get("error") or "Could not post card to channel.")

    CardRequest.objects.create(
        organization_id=_org_id(org),
        card_id=card_id,
        status=CardRequest.Status.SENT,
        requested_by_email=by_user.email,
        sent_at=timezone.now(),
    )
    return result


def schedule_card(org: Organization, *, card_id: str, by_email: str, scheduled_at: datetime) -> CardRequest:
    _require_card(card_id)
    if scheduled_at <= timezone.now():
        raise CardsError("invalid_schedule", "Scheduled time must be in the future.")
    quota = quota_for_org(org)
    if quota["used"] >= quota["max"]:
        raise CardsError("quota_exceeded", f"Monthly quota of {quota['max']} cards reached.")
    return CardRequest.objects.create(
        organization_id=_org_id(org),
        card_id=card_id,
        status=CardRequest.Status.SCHEDULED,
        requested_by_email=by_email,
        scheduled_at=scheduled_at,
    )


def request_new_card(org: Organization, *, by_email: str, title: str, description: str = "") -> CardCustomRequest:
    if not title.strip():
        raise CardsError("title_required", "Give the new card a title.")
    return CardCustomRequest.objects.create(
        organization_id=_org_id(org),
        requested_by_email=by_email,
        title=title.strip(),
        description=description.strip(),
    )


def dispatch_due_scheduled_cards(*, now=None) -> dict:
    """Send every CardRequest whose scheduled_at has arrived. Meant to be
    called periodically by an external scheduler (e.g. a Render Cron Job)
    via `python manage.py dispatch_scheduled_cards` — Django itself has no
    way to run code at a future time on its own."""
    from .teams_messaging import send_channel_adaptive_card

    now = now or timezone.now()
    due = list(CardRequest.objects.filter(status=CardRequest.Status.SCHEDULED, scheduled_at__lte=now))
    sent, failed = 0, 0
    for row in due:
        card = CARD_BY_ID.get(row.card_id)
        org = Organization.objects.filter(pk=row.organization_id).first()
        user = (
            AIDLUser.objects.filter(email__iexact=row.requested_by_email, is_active=True).first()
            if row.requested_by_email
            else None
        )
        if card is None or org is None or user is None:
            row.status = CardRequest.Status.FAILED
            row.error = "missing_card_org_or_user"
            row.save(update_fields=["status", "error", "updated_at"])
            failed += 1
            continue

        team_id = getattr(user, "teams_team_id", "") or ""
        channel_id = org.teams_welcome_channel_id or getattr(user, "teams_channel_id", "") or ""
        access_token = get_access_token_for_user(user) if team_id and channel_id else ""
        if not access_token:
            row.error = "missing_refresh_token_or_channel"
            row.save(update_fields=["error", "updated_at"])
            failed += 1
            continue

        result = send_channel_adaptive_card(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            card=build_reference_card_message(card),
        )
        if result.get("ok"):
            row.status = CardRequest.Status.SENT
            row.sent_at = timezone.now()
            row.error = ""
            row.save(update_fields=["status", "sent_at", "error", "updated_at"])
            sent += 1
        else:
            row.error = (result.get("detail") or result.get("error") or "")[:255]
            row.save(update_fields=["error", "updated_at"])
            failed += 1
            logger.warning("scheduled card send failed for %s/%s: %s", row.organization_id, row.card_id, row.error)

    return {"checked": len(due), "sent": sent, "failed": failed}
