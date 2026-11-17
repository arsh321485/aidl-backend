"""Step 5: the Admin Center for Google Classroom admins (core part).

Same rules as the Slack / Teams Admin Center; this file only calls api/:

    home()          the 5 setup steps (policy · admins · team · card approval · start),
                    plan and seats
    team()          each learner's progress (licensing.team_progress) + their classes
    remove_learner / restore_learner
    add_admin / remove_admin / set_permissions   (admin_perms rules: the master
                    admin can't be removed or changed; nobody removes themselves;
                    you can only give permissions you have; admin seat limit)

api/admin_setup.summary() counts only Slack people as "the team", so the team
numbers here come from licensing.learners() (everyone in the organization who
isn't an admin), which is what Google Classroom learners are.

Not here yet (next part): policy cards, own-AUP upload, regulators, AI / IT
apps, logo, alerts.
"""

from __future__ import annotations

from django.utils import timezone

from api import admin_perms, admin_setup, card_approval, licensing
from api.models import AIDLUser, Organization
from api.plans import seat_holders, usage, user_limit

from .models import GoogleCourse, GoogleStudent


def _forget_primary(org: Organization) -> None:
    from api.slack_onboarding import forget_primary_admin

    forget_primary_admin(org)


def can(org: Organization, user: AIDLUser) -> dict:
    """What this admin may do (admin_perms)."""
    return {
        "master": admin_perms.is_master(org, user),
        "admins": admin_perms.has(org, user, "manage_admins"),
        "billing": admin_perms.has(org, user, "billing"),
        "cards": admin_perms.has(org, user, "approve_cards"),  # approve cards, start the programme
    }


def home(org: Organization, user: AIDLUser) -> dict:
    st = admin_setup.summary(org)
    people = licensing.learners(org)
    members, licensed = people.count(), people.filter(licence_issued=True).count()
    admins = AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True).count()
    steps = {
        "policy": st["aup_done"],
        "admins": bool(admin_setup.state(org).get("admins_step")) or admins > 1,
        "team": members > 0,
        "approval": card_approval.onboarding_ready(org),
        "started": admin_setup.started(org),
    }
    done = sum(steps.values())
    return {
        "steps": steps, "done_count": done, "bar": range(done), "rest": range(5 - done),
        "onboarding": card_approval.onboarding(org),
        "members": members, "licensed": licensed, "admins_count": admins,
        "classes_in_aidl": GoogleCourse.objects.filter(organization_id=str(org.pk), in_programme=True).count(),
        "plan": usage(org), "can": can(org, user),
    }


# --- team ---

def team(org: Organization) -> dict:
    data = licensing.team_progress(org)
    names = {c.course_id: c.name for c in GoogleCourse.objects.filter(organization_id=str(org.pk))}
    classes: dict[str, list[str]] = {}
    for s in GoogleStudent.objects.filter(organization_id=str(org.pk)).exclude(aidl_user_id=""):
        if s.course_id in names:
            classes.setdefault(s.aidl_user_id, []).append(names[s.course_id])
    for row in data["rows"]:
        row["classes"] = sorted(set(classes.get(str(row["member"].pk), [])))
        row["status_label"] = licensing.STATUS_LABELS[row["status"]]
    data["removed_people"] = list(licensing.removed_learners(org).order_by("full_name"))
    data["plan"] = usage(org)
    return data


def _person(org: Organization, user_id: str) -> AIDLUser | None:
    return AIDLUser.objects.filter(pk=user_id, organization_id=str(org.pk), is_active=True).first() if user_id else None


def remove_learner(org: Organization, by: AIDLUser, user_id: str) -> str:
    """Take a learner out of the programme (any admin). Their licence is
    suspended and, if they earned one, still counts toward the plan."""
    from api.package_delivery import pause_package

    target = _person(org, user_id)
    if target is None or target.role == AIDLUser.Role.ADMIN:
        return "That person isn't a learner in your programme."
    name = target.full_name or target.email
    if target.slack_left_at:  # the "removed from the programme" field, shared by every platform
        return f"{name} was already removed."
    target.slack_left_at = timezone.now()
    target.save(update_fields=["slack_left_at", "updated_at"])
    pause_package(org, target)
    GoogleStudent.objects.filter(organization_id=str(org.pk), aidl_user_id=str(target.pk)).update(aidl_status="removed")
    if target.licence_issued:
        return f"{name} was removed. Their licence {target.licence_number} is suspended and still counts toward your plan."
    return f"{name} was removed and their seat is free again."


def restore_learner(org: Organization, by: AIDLUser, user_id: str) -> str:
    target = _person(org, user_id)
    if target is None or target.role == AIDLUser.Role.ADMIN or not target.slack_left_at:
        return "That person isn't removed."
    name = target.full_name or target.email
    if not target.licence_issued and seat_holders(org).count() >= user_limit(org):
        return f"Your plan has no free seat, so {name} can't be added back. Upgrade your plan or remove someone first."
    target.slack_left_at = None
    target.save(update_fields=["slack_left_at", "updated_at"])
    GoogleStudent.objects.filter(organization_id=str(org.pk), aidl_user_id=str(target.pk)).update(aidl_status="added")
    return f"{name} is back in the programme."


# --- admins ---

def admins(org: Organization, user: AIDLUser) -> dict:
    master = admin_perms.is_master(org, user)
    rows = []
    for a in AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True
                                     ).order_by("full_name"):
        is_master = admin_perms.is_master(org, a)
        rows.append({"person": a, "master": is_master, "me": a.pk == user.pk,
                     "labels": [admin_perms.MASTER_LABEL] if is_master else admin_perms.labels(a),
                     "perms": {k: bool(getattr(a, f, False)) for k, f, _l, _d in admin_perms.PERMS}})
    allowed = admin_perms.grantable(org, user)
    return {
        "rows": rows,
        "candidates": list(licensing.learners(org).order_by("full_name")),
        "perms": [{"key": k, "label": l, "desc": d, "allowed": k in allowed} for k, _f, l, d in admin_perms.PERMS],
        "seat_limit": org.admin_seat_limit, "is_master": master,
        "full": len(rows) >= (org.admin_seat_limit or 3),
    }


def add_admin(org: Organization, by: AIDLUser, user_id: str, chosen: list[str]) -> str:
    if not admin_perms.has(org, by, "manage_admins"):
        return "Only the master admin, or admins allowed to manage admins, can add admins."
    target = _person(org, user_id)
    if target is None:
        return "Pick someone from your programme."
    if target.role == AIDLUser.Role.ADMIN:
        return f"{target.full_name or target.email} is already an admin."
    count = AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True).count()
    if count >= (org.admin_seat_limit or 3):
        return f"Your plan allows {org.admin_seat_limit} admins. Remove an admin first."
    target.role = AIDLUser.Role.ADMIN
    for field in admin_perms.FIELDS:
        setattr(target, field, False)
    admin_perms.apply(target, chosen, admin_perms.grantable(org, by))
    target.save(update_fields=["role", *admin_perms.FIELDS, "updated_at"])
    _forget_primary(org)
    return f"{target.full_name or target.email} is now an AIDL admin."


def set_permissions(org: Organization, by: AIDLUser, user_id: str, chosen: list[str]) -> str:
    if not admin_perms.has(org, by, "manage_admins"):
        return "Only the master admin, or admins allowed to manage admins, can change permissions."
    target = _person(org, user_id)
    if target is None or target.role != AIDLUser.Role.ADMIN:
        return "That person isn't an admin."
    if admin_perms.is_master(org, target):
        return "The master admin always has every permission."
    admin_perms.apply(target, chosen, admin_perms.grantable(org, by))
    target.save(update_fields=[*admin_perms.FIELDS, "updated_at"])
    return f"Permissions for {target.full_name or target.email} saved."


def remove_admin(org: Organization, by: AIDLUser, user_id: str) -> str:
    if not admin_perms.has(org, by, "manage_admins"):
        return "Only the master admin, or admins allowed to manage admins, can remove admins."
    target = _person(org, user_id)
    if target is None or target.role != AIDLUser.Role.ADMIN:
        return "That person isn't an admin any more."
    if admin_perms.is_master(org, target):
        return "The master admin can't be removed."
    if target.pk == by.pk:
        return "You can't remove yourself. Ask another admin."
    name = target.full_name or target.email
    target.role = AIDLUser.Role.LEARNER
    for field in admin_perms.FIELDS:
        setattr(target, field, False)
    target.save(update_fields=["role", *admin_perms.FIELDS, "updated_at"])
    _forget_primary(org)
    # Now a learner, so they need a seat.
    if target.slack_left_at is None and seat_holders(org).count() > user_limit(org):
        target.slack_left_at = timezone.now()
        target.save(update_fields=["slack_left_at", "updated_at"])
        return f"{name} is no longer an admin. Your plan has no free seat, so they're not in the programme either."
    return f"{name} is no longer an admin. They're now a learner and use one of your plan's seats."
