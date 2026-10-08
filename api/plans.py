"""AIDL plans — platform-neutral.

Packages come from the manager's AI_Usage_Awareness_Packages.xlsx, exported
to api/data/awareness_packages.json (Default = 12 cards, Basic = 24 cards,
a superset of Default, both "post in the order shown", two posts a week).

User limit rules:
- Every learner in the AIDL channel holds a seat (admins never do).
- A learner who earned a licence keeps holding that seat even after they
  are removed, so removing licensed people and adding new ones can't hand
  out more licences than the plan allows. Removing someone who never earned
  a licence frees their seat.
- Someone added to the channel when the plan is full is removed again and
  never gets an account or a licence.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from django.db.models import Q

from .models import AIDLUser, Organization

PLANS = {
    "trial": {"label": "Trial", "package": "default", "max_users": 5, "cards_per_week": 2},
    "basic": {"label": "Basic", "package": "basic", "max_users": None, "cards_per_week": 2},
}
PACKAGE_LABELS = {"default": "Default package", "basic": "Basic package"}
_PACKAGES_FILE = Path(__file__).resolve().parent / "data" / "awareness_packages.json"


@lru_cache(maxsize=1)
def _packages() -> dict:
    with open(_PACKAGES_FILE, encoding="utf-8") as fh:
        return json.load(fh)["packages"]


def plan_of(org: Organization) -> dict:
    return PLANS.get(org.plan) or PLANS["trial"]


def package_cards(org: Organization) -> list[dict]:
    """The plan's package cards, in posting order."""
    return _packages()[plan_of(org)["package"]]


def user_limit(org: Organization) -> int:
    return plan_of(org)["max_users"] or org.seats_purchased


def seat_holders(org: Organization):
    """Learners using a seat: everyone in the channel, plus anyone who left
    after earning a licence."""
    return (AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True)
            .exclude(role=AIDLUser.Role.ADMIN)
            .filter(Q(slack_left_at__isnull=True) | Q(licence_issued=True)))


def holds_seat(member: AIDLUser | None) -> bool:
    if member is None or member.role == AIDLUser.Role.ADMIN or not member.is_active:
        return False
    return member.slack_left_at is None or member.licence_issued


def can_join(org: Organization, member: AIDLUser | None) -> bool:
    """May this person (None = not known to AIDL yet) be part of the team?"""
    if member is not None and member.role == AIDLUser.Role.ADMIN:
        return True
    if holds_seat(member):
        return True  # already counted — e.g. a licensed member added back
    return seat_holders(org).count() < user_limit(org)


def usage(org: Organization) -> dict:
    plan = plan_of(org)
    limit = user_limit(org)
    used = seat_holders(org).count()
    return {
        "plan": org.plan if org.plan in PLANS else "trial",
        "label": plan["label"],
        "package": plan["package"],
        "package_label": PACKAGE_LABELS[plan["package"]],
        "cards": len(package_cards(org)),
        "cards_per_week": plan["cards_per_week"],
        "limit": limit,
        "used": used,
        "free": max(limit - used, 0),
        "full": used >= limit,
    }
