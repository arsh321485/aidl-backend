"""Licence rules — platform-neutral (Slack today, Teams later).

A user earns the Learner (L) licence by acknowledging the Highway Code and
the Traffic Light Check. Nobody issues licences by hand; admins only see
the result. The organization's seat limit still applies.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from .models import Acknowledgement, AIDLUser, Organization

# Things a user must acknowledge for the Learner licence, and their content
# version (bump when the text changes so people re-acknowledge).
LEARNER_REQUIREMENTS = ("highway_code", "traffic_light")
ACK_VERSION = "v1"
ACK_LABELS = {"highway_code": "📖 Highway Code", "traffic_light": "🚦 Traffic Light Check", "aup": "📄 AUP"}
# Items a user can accept. The AUP is versioned by its content (aup.py); it is
# not (yet) required for the Learner licence.
ACK_ITEMS = LEARNER_REQUIREMENTS + ("aup",)


def acknowledged_items(member: AIDLUser) -> set[str]:
    return set(
        Acknowledgement.objects.filter(user_id=str(member.pk), version=ACK_VERSION).values_list("item", flat=True)
    )


def acknowledged_at(member: AIDLUser, item: str, version: str = ACK_VERSION):
    row = Acknowledgement.objects.filter(user_id=str(member.pk), item=item, version=version).first()
    return row.created_at if row else None


def acknowledge(member: AIDLUser, item: str, version: str = ACK_VERSION) -> bool:
    """Record that the user accepted `item` (at `version`). Returns False if
    they already had."""
    if item not in ACK_ITEMS:
        raise ValueError(f"unknown item {item!r}")
    try:
        Acknowledgement.objects.create(
            organization_id=member.organization_id, user_id=str(member.pk), item=item, version=version
        )
    except IntegrityError:
        return False
    if item == "aup":
        from django.utils import timezone as tz

        member.aup_signed = True
        member.aup_signed_at = tz.now()
        member.save(update_fields=["aup_signed", "aup_signed_at", "updated_at"])
    return True


def missing_for_learner(member: AIDLUser) -> list[str]:
    done = acknowledged_items(member)
    return [item for item in LEARNER_REQUIREMENTS if item not in done]


def learners(org: Organization):
    """The team: everyone in the organization except admins (IT / HR people
    who run AIDL rather than take part in it)."""
    return (AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True, slack_left_at__isnull=True)
            .exclude(role=AIDLUser.Role.ADMIN))


def removed_learners(org: Organization):
    """Learners taken out of the AIDL channel (licence suspended)."""
    return (AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True, slack_left_at__isnull=False)
            .exclude(role=AIDLUser.Role.ADMIN))


def seats_left(org: Organization) -> int:
    """Licences the plan still allows. Licences of removed members still
    count, so removing and re-adding people can't exceed the plan."""
    from .plans import user_limit

    issued = (AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True, licence_issued=True)
              .exclude(role=AIDLUser.Role.ADMIN).count())
    return max(user_limit(org) - issued, 0)


def new_licence_number() -> str:
    for _ in range(5):
        number = f"AIDL-L-{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
        if not AIDLUser.objects.filter(licence_number=number).exists():
            return number
    return f"AIDL-L-{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"


def issue_learner_if_ready(member: AIDLUser, org: Organization) -> str:
    """'issued', 'already', 'left', 'not_ready' or 'no_seats'."""
    if member.licence_issued:
        return "already"
    if member.slack_left_at:
        return "left"
    if missing_for_learner(member):
        return "not_ready"
    if seats_left(org) <= 0:
        return "no_seats"
    now = timezone.now()
    member.licence_issued = True
    member.licence_number = member.licence_number or new_licence_number()
    member.licence_issued_at = now
    member.licence_expires_at = now + timedelta(days=365)
    member.save(update_fields=["licence_issued", "licence_number", "licence_issued_at", "licence_expires_at", "updated_at"])
    return "issued"


def issue_individual_licence(member: AIDLUser) -> None:
    """Website individuals: Learner (L) licence on sign-up, valid 365 days.
    (Organization members earn theirs in Slack — issue_learner_if_ready.)"""
    if member.licence_issued:
        return
    now = timezone.now()
    member.licence_issued = True
    member.license_class = AIDLUser.LicenseClass.CLASS_L
    member.licence_number = member.licence_number or new_licence_number()
    member.licence_issued_at = now
    member.licence_expires_at = now + timedelta(days=365)
    member.save(update_fields=["licence_issued", "license_class", "licence_number", "licence_issued_at",
                               "licence_expires_at", "updated_at"])


# ---------- admin progress view ----------

STATUS_LABELS = {
    "licensed": "🪪 Licensed",
    "legacy": "🪪 Licensed (before acknowledgement rule)",
    "waiting_seat": "⏳ Waiting for a seat",
    "in_progress": "🟡 In progress",
    "not_started": "⬜ Not started",
}


def member_status(member: AIDLUser, done: set[str]) -> str:
    complete = all(item in done for item in LEARNER_REQUIREMENTS)
    if member.licence_issued:
        # Licences from the old Add User flow were issued without acknowledgements.
        return "licensed" if complete else "legacy"
    if complete:
        return "waiting_seat"
    return "in_progress" if done else "not_started"


def team_progress(org: Organization) -> dict:
    """Each team member's journey: joined → Highway Code → Traffic Light →
    licence. Admins are not part of the team."""
    members = list(learners(org).order_by("full_name"))
    from .aup import generate_aup

    aup_version = generate_aup(org)["version"]
    acks: dict[str, dict[str, object]] = {}
    for row in Acknowledgement.objects.filter(organization_id=str(org.pk), version__in=[ACK_VERSION, aup_version]):
        acks.setdefault(row.user_id, {})[row.item] = row.created_at
    rows = []
    for m in members:
        done = acks.get(str(m.pk), {})
        rows.append({
            "member": m,
            "name": m.full_name or m.email,
            "email": m.email,
            "role": m.role,
            "highway_code_at": done.get("highway_code"),
            "traffic_light_at": done.get("traffic_light"),
            "aup_at": done.get("aup"),
            "status": member_status(m, set(done)),
        })
    count = lambda pred: sum(1 for r in rows if pred(r))  # noqa: E731
    return {
        "rows": rows,
        "members": len(rows),
        "highway_code": count(lambda r: r["highway_code_at"]),
        "traffic_light": count(lambda r: r["traffic_light_at"]),
        "aup": count(lambda r: r["aup_at"]),
        "aup_version": aup_version,
        "licensed": count(lambda r: r["status"] in ("licensed", "legacy")),
        "legacy": count(lambda r: r["status"] == "legacy"),
        "waiting_seat": count(lambda r: r["status"] == "waiting_seat"),
        "not_started": count(lambda r: r["status"] == "not_started"),
        "removed": [{"name": m.full_name or m.email, "email": m.email, "licensed": m.licence_issued,
                     "left_at": m.slack_left_at} for m in removed_learners(org).order_by("full_name")],
    }
