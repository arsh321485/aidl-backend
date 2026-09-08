"""
Dynamic "Send Cards" data — merges the fixed catalog (card_catalog.py) with
per-organisation state stored in Mongo (CardDelivery / CardRequest).

This is what makes the Cards tab/card stop "repeating": state is looked up
by organization_id every time, never held only in a browser tab or reset by
a fresh login from the same email — whoever opens it sees the real current
status.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .card_catalog import CARD_CATALOG, CARD_ORDER, QUOTA_MAX
from .models import CardDelivery, CardRequest, Organization


def _org_id(org: Organization) -> str:
    return str(org.pk)


def _month_start(now=None):
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def get_deliveries_map(org: Organization) -> dict[str, CardDelivery]:
    org_id = _org_id(org)
    rows = CardDelivery.objects.filter(organization_id=org_id)
    return {row.card_key: row for row in rows}


def compute_quota_used(org: Organization) -> int:
    """
    Cards requested (catalog "Request Card" or a custom request) this
    calendar month — matches the design's quota meaning, not send status.
    """
    org_id = _org_id(org)
    start = _month_start()
    requested_catalog = CardDelivery.objects.filter(
        organization_id=org_id,
        purchased=True,
        created_at__gte=start,
    ).count()
    custom_requests = CardRequest.objects.filter(
        organization_id=org_id,
        created_at__gte=start,
    ).count()
    return requested_catalog + custom_requests


def build_cards_payload(org: Organization) -> dict:
    """Full dynamic 'Send Cards' payload for the Cards tab / API."""
    deliveries = get_deliveries_map(org)
    items = []
    for key in CARD_ORDER:
        meta = CARD_CATALOG[key]
        row = deliveries.get(key)
        status = row.status if row else CardDelivery.Status.NOT_SENT
        purchased = bool(row and row.purchased)
        lights = (row.lights.split(",") if row and row.lights else ["green", "amber", "red"])
        lights = [item for item in lights if item]
        items.append(
            {
                "key": key,
                "icon": meta["icon"],
                "kicker": meta["kicker"],
                "title": meta["title"],
                "price": meta["price"],
                "rating": meta["rating"],
                "votes": meta["votes"],
                "desc": meta["desc"],
                "body": meta["body"],
                "has_lights": bool(meta.get("has_lights")),
                "status": status,
                "purchased": purchased,
                "lights": lights,
                "sent_at": row.sent_at.isoformat() if row and row.sent_at else None,
                "scheduled_at": row.scheduled_at.isoformat() if row and row.scheduled_at else None,
            }
        )

    custom_requests = [
        {
            "name": req.name,
            "description": req.description,
            "priority": req.priority,
            "status": req.status,
            "created_at": req.created_at.isoformat(),
        }
        for req in CardRequest.objects.filter(organization_id=_org_id(org))
    ]

    quota_used = compute_quota_used(org)
    return {
        "quota_used": quota_used,
        "quota_max": QUOTA_MAX,
        "quota_available_text": "All cards available this billing period",
        "items": items,
        "custom_requests": custom_requests,
    }


def act_on_card(
    org: Organization,
    card_key: str,
    action: str,
    *,
    lights: list[str] | None = None,
    scheduled_at=None,
) -> CardDelivery:
    """
    action: "request" | "send" | "schedule"
    Idempotent per (org, card_key) — repeated calls update the same row
    instead of creating duplicates, which is exactly what keeps the tab
    from "repeating" when the same admin opens it again.
    """
    if card_key not in CARD_CATALOG:
        raise ValueError(f"unknown card_key: {card_key}")

    row, _created = CardDelivery.objects.get_or_create(
        organization_id=_org_id(org),
        card_key=card_key,
    )

    if action == "request":
        row.purchased = True
    elif action == "send":
        row.purchased = True
        row.status = CardDelivery.Status.SENT
        row.sent_at = timezone.now()
        row.scheduled_at = None
        if lights:
            row.lights = ",".join(lights)
    elif action == "schedule":
        row.purchased = True
        row.status = CardDelivery.Status.SCHEDULED
        row.scheduled_at = scheduled_at
        if lights:
            row.lights = ",".join(lights)
    else:
        raise ValueError(f"unknown action: {action}")

    row.save()
    return row


def create_card_request(
    org: Organization,
    *,
    name: str,
    description: str = "",
    priority: str = CardRequest.Priority.STANDARD,
    requested_by_email: str = "",
) -> CardRequest:
    return CardRequest.objects.create(
        organization_id=_org_id(org),
        name=name,
        description=description or "No additional details provided.",
        priority=priority,
        requested_by_email=requested_by_email,
    )
