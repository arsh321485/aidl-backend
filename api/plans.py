"""AIDL plans — platform-neutral.

Cards come from the manager's AI_Usage_Awareness_Packages.xlsx, exported to
api/data/awareness_packages.json (`python manage.py import_awareness_packages`):
the Default package (12), the Basic package (24) and the full library
(655 cards in 15 topics / categories).

    Trial       Default 12 cards                         1 card a month (a year)
    Basic       2 a week for a year = 104 cards:         2 a week (Mon, Thu)
                Default 12 + 92 from topics the admin
                picks (no picks: the manager's Basic 24
                first)
    Enterprise  4 a week for a year = 208 cards:         4 a week, on days and
                Default 12 + 196 from all 15 topics      at a time the admin picks

When the picked topics run out of cards, the rest come from all topics.

Topic picks and the Enterprise schedule live in Organization.billing
(billing.py), set when the admin upgrades on the website.

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

ORDER = ("trial", "basic", "enterprise")
WEEKS_A_YEAR = 52
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PLANS = {
    "trial": {"label": "Trial", "max_users": 5, "schedule": {"monthly": True, "hour": 10},
              "cadence": "1 card a month", "picks_topics": False, "all_topics": False},
    "basic": {"label": "Basic", "max_users": None, "schedule": {"days": (0, 3), "hour": 10},
              "cadence": "2 cards a week (Mon, Thu)", "picks_topics": True, "all_topics": False},
    "enterprise": {"label": "Enterprise", "max_users": None, "schedule": {"days": (0, 1, 3, 4), "hour": 10},
                   "cadence": "4 cards a week, on your schedule", "picks_topics": False, "all_topics": True,
                   "custom_schedule": True},
}
PACKAGE_LABELS = {"trial": "Default package", "basic": "Basic package", "enterprise": "Enterprise package"}
_PACKAGES_FILE = Path(__file__).resolve().parent / "data" / "awareness_packages.json"


@lru_cache(maxsize=1)
def _data() -> dict:
    with open(_PACKAGES_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def _packages() -> dict:
    return _data()["packages"]


def categories() -> list[dict]:
    return _data().get("categories", [])


def library() -> list[dict]:
    return _data().get("library", [])


def plan_key(org: Organization) -> str:
    return org.plan if org.plan in PLANS else "trial"


def plan_of(org: Organization) -> dict:
    return PLANS[plan_key(org)]


def rank(plan: str) -> int:
    return ORDER.index(plan) if plan in ORDER else 0


# ---------- cards ----------

def total_cards(plan: str) -> int:
    """Cards a plan delivers: the Default 12 on Trial, a year of cards on
    the weekly plans."""
    if plan == "trial":
        return len(_packages()["default"])
    return len(PLANS[plan]["schedule"]["days"]) * WEEKS_A_YEAR


def all_topic_codes() -> list[str]:
    return [c["code"] for c in categories()]


def pick_cards(topic_codes: list[str], exclude: set[str], n: int) -> list[dict]:
    """n library cards from the chosen topics, taken in turn from each topic
    (library order), skipping cards already in the package."""
    pools = {code: [c for c in library() if c["code"] == code and c["no"] not in exclude] for code in topic_codes}
    picked: list[dict] = []
    while len(picked) < n and any(pools.values()):
        for code in topic_codes:
            if pools[code] and len(picked) < n:
                picked.append(pools[code].pop(0))
    return picked


def _billing(org: Organization) -> dict:
    try:
        return json.loads(org.billing or "{}")
    except ValueError:
        return {}


def topics_for(org: Organization, plan: str) -> list[str]:
    return list((_billing(org).get("topics") or {}).get(plan) or [])


def cards_for_plan(org: Organization, plan: str, topics: dict[str, list[str]] | None = None) -> list[dict]:
    """The cards a plan delivers, in posting order. `topics` overrides the
    saved picks (used for the website preview)."""
    def picks(p):
        return topics[p] if topics and p in topics else topics_for(org, p)

    cards = list(_packages()["default"])
    if plan == "trial":
        return cards
    total = total_cards(plan)
    if plan == "enterprise":
        chosen = all_topic_codes()
    else:
        chosen = picks("basic")
        if not chosen:  # no topics picked: the manager's Basic package first
            cards += [c for c in _packages()["basic"] if c["no"] not in {d["no"] for d in cards}]
            chosen = all_topic_codes()
    have = {c["no"] for c in cards}
    cards += pick_cards(chosen, have, total - len(cards))
    if len(cards) < total:  # the picked topics ran out of cards
        cards += pick_cards(all_topic_codes(), {c["no"] for c in cards}, total - len(cards))
    return cards[:total]


def package_cards(org: Organization) -> list[dict]:
    """The organization's package cards, in posting order."""
    return cards_for_plan(org, plan_key(org))


def schedule_of(org: Organization) -> dict:
    """{"monthly": True, "hour"} or {"days": (...), "hour"} — Enterprise may
    have its own days and time."""
    base = dict(plan_of(org)["schedule"])
    custom = _billing(org).get("schedule") or {}
    if plan_of(org).get("custom_schedule") and custom.get("days"):
        base = {"days": tuple(custom["days"]), "hour": int(custom.get("hour", base["hour"]))}
    return base


def cadence(org: Organization) -> str:
    s = schedule_of(org)
    if s.get("monthly"):
        return "1 card a month"
    return f"{len(s['days'])} cards a week ({', '.join(WEEKDAYS[d] for d in s['days'])}, {s['hour']:02d}:00)"


# ---------- users ----------

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
    key = plan_key(org)
    limit = user_limit(org)
    used = seat_holders(org).count()
    return {
        "plan": key,
        "label": PLANS[key]["label"],
        "package": key,
        "package_label": PACKAGE_LABELS[key],
        "cards": len(package_cards(org)),
        "cadence": cadence(org),
        "limit": limit,
        "used": used,
        "free": max(limit - used, 0),
        "full": used >= limit,
        "can_upgrade": key != ORDER[-1],
    }
