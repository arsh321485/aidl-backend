"""Who each plan lets into AIDL, in Google Classroom (demo rules, 2026-11-19).

    Plan        Teachers (AIDL admins)      Students (learners)
    Trial       2                           5
    Basic       10                          none: a teachers-only plan
    Enterprise  no limit                    no limit: the whole school, any number

- Teachers = AIDL admins. A teacher takes the course by joining the AIDL class
  as a student with their own account (learners.py lets admins answer cards).
- Students = everyone else in the AIDL class. Each one holds a seat (api.plans
  rules: a licensed learner keeps their seat even after being removed).
- Nobody is taken out when a plan changes; the limits only stop new people.
  When a plan allows more, waiting students join on the next sync.

These live here, not in api/plans.py, so only Google Classroom changes for the
demo. Moving LIMITS into api/plans.py later makes Slack and Teams follow them too.
"""

from __future__ import annotations

from api.models import AIDLUser, Organization
from api import plans
from api.plans import holds_seat, plan_key, seat_holders

LIMITS = {
    "trial": {"teachers": 2, "students": 5},
    "basic": {"teachers": 10, "students": 0},
    "enterprise": {"teachers": None, "students": None},  # None = no limit
}

# Enterprise has no student limit, but the shared licence code (api.licensing.seats_left,
# api.plans.can_join) counts seats bought. An Enterprise school's seats are kept at least this
# high (unlimited_seats), so those never run out; Classroom shows "unlimited".
UNLIMITED_SEATS = 1_000_000


def unlimited_seats(org: Organization) -> None:
    if plan_key(org) == "enterprise" and (org.seats_purchased or 0) < UNLIMITED_SEATS:
        org.seats_purchased = UNLIMITED_SEATS
        org.save(update_fields=["seats_purchased", "updated_at"])


def limits(org: Organization) -> dict:
    return LIMITS.get(plan_key(org), LIMITS["trial"])


def teachers_used(org: Organization) -> int:
    return AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True).count()


def can_add_teacher(org: Organization) -> bool:
    limit = limits(org)["teachers"]
    return limit is None or teachers_used(org) < limit


def student_room(org: Organization) -> int:
    """How many more students the plan has seats for (Enterprise: the seats bought)."""
    if plan_key(org) == "enterprise":
        return UNLIMITED_SEATS
    limit = limits(org)["students"]
    if limit == 0:
        return 0
    cap = plans.user_limit(org) if limit is None else min(limit, plans.user_limit(org))
    return max(cap - seat_holders(org).count(), 0)


def teacher_room(org: Organization, row, token: str) -> int | None:
    """How many more teachers the plan allows in the AIDL class (None = no limit). Counts
    whichever is more: the class's teachers in Google Classroom, or AIDL's admins."""
    from . import classroom_api

    limit = limits(org)["teachers"]
    if limit is None:
        return None
    try:
        in_class = len(classroom_api.list_teachers(token, row.course_id))
    except Exception:  # noqa: BLE001 - can't read the class: count AIDL's admins only
        in_class = 0
    return limit - max(in_class, teachers_used(org))


def teacher_refusal(org: Organization) -> str:
    from api.plans import PLANS

    return (f"{org.name}'s AIDL {PLANS[plan_key(org)]['label']} plan allows {limits(org)['teachers']} teachers. "
            "Upgrade the plan to add more.")


def can_add_student(org: Organization, member: AIDLUser | None) -> bool:
    """May this person (None = not in AIDL yet) be a learner?"""
    if member is not None and member.role == AIDLUser.Role.ADMIN:
        return True  # a teacher taking the course
    if holds_seat(member):
        return True  # already counted
    if plan_key(org) == "enterprise":
        return True  # the whole school
    limit = limits(org)["students"]
    if limit == 0:
        return False
    if limit is not None and seat_holders(org).count() >= limit:
        return False
    return plans.can_join(org, member)  # and the seats the organization bought


def refusal(org: Organization) -> str:
    """Why a new student can't join, for the student's private post."""
    from api.plans import PLANS

    label = PLANS[plan_key(org)]["label"]
    if limits(org)["students"] == 0:
        return (f"{org.name}'s AIDL {label} plan is for teachers only, so students can't take the AI Driving "
                "Licence yet. Your teacher has been told.")
    return (f"{org.name}'s AIDL {label} plan is full, so you can't start your AI Driving Licence yet. "
            "Your teacher has been told; you'll get your cards as soon as there's room.")


def summary(org: Organization) -> dict:
    """For the Admin Center."""
    from api.plans import PLANS, user_limit

    lim = limits(org)
    students_limit = lim["students"]
    if students_limit is None and plan_key(org) != "enterprise":
        students_limit = user_limit(org)  # None = unlimited (Enterprise)
    return {
        "label": PLANS[plan_key(org)]["label"],
        "teachers_used": teachers_used(org), "teachers_limit": lim["teachers"],
        "students_used": seat_holders(org).count(), "students_limit": students_limit,
        "teachers_only": lim["students"] == 0,
    }
