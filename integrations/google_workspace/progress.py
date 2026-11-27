"""The Google Workspace Admin Center's "Your team" list: everyone the admin
invited or who joined, where each person is, and reminders.

    rows(org)            one dict per person, newest problems first
    remind(org, key)     re-send that person's current email (invite or step)
    remind_all(org)      everyone who hasn't finished and wasn't reminded lately

A person is reminded at most once per REMIND_EVERY, so a busy admin can't
flood anyone's inbox. Reminder emails are the same emails as before (mail.py).
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from api.models import AIDLUser, Organization

from . import mail
from .models import WorkspaceInvite, WorkspaceMember

REMIND_EVERY = timedelta(hours=24)

# step (mail.step_of) -> (label, tone). Tones: "ok" licensed, "go" in progress, "" waiting.
STEP_LABELS = {
    "welcome": ("Hasn't opened the Welcome email", ""),
    "policy": ("Reading the AI policy", "go"),
    "traffic_light": ("On the Traffic Light Check", "go"),
    "highway_code": ("On the Highway Code", "go"),
    "avatar": ("Making their licence avatar", "go"),
    "licence": ("Licensed", "ok"),
}


def _recent(when) -> bool:
    return bool(when) and timezone.now() - when < REMIND_EVERY


def _member_row(user: AIDLUser, org: Organization, started: bool, sent: dict) -> dict:
    row = {"key": f"u:{user.pk}", "name": user.full_name or user.email, "email": user.email,
           "can_remind": False, "reminded": False}
    if user.licence_issued:
        when = user.licence_issued_at.strftime("%d %b %Y") if user.licence_issued_at else ""
        return {**row, "status": f"Licensed{f' · {when}' if when else ''}", "tone": "ok", "order": 3}
    if not started:
        return {**row, "status": "Joined · waiting for you to start", "tone": "", "order": 2}
    step = mail.step_of(user, org)
    if not step:
        return {**row, "status": "Waiting for the next card to be approved", "tone": "", "order": 1}
    label, tone = STEP_LABELS[step]
    if step == "policy":
        from api import aup_store

        left = len(aup_store.pending_points(user, org))
        label += f" · {left} point{'' if left == 1 else 's'} left"
    member = sent.get(str(user.pk))
    reminded = _recent(member.reminded_at) if member else False
    return {**row, "status": label, "tone": tone, "order": 0, "step": step,
            "can_remind": not reminded, "reminded": reminded}


def rows(org: Organization) -> list[dict]:
    from api import admin_setup
    from api.licensing import learners

    started = admin_setup.started(org)
    people = list(learners(org))
    sent = {m.user_id: m for m in WorkspaceMember.objects.filter(user_id__in=[str(p.pk) for p in people])}
    out = [_member_row(p, org, started, sent) for p in people]
    joined = {p.email.lower() for p in people} | {  # admins aren't learners but did join
        e.lower() for e in AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN)
        .values_list("email", flat=True)}
    for inv in WorkspaceInvite.objects.filter(organization_id=str(org.pk)).order_by("-invited_at"):
        if inv.email.lower() in joined:
            continue
        out.append({"key": f"i:{inv.pk}", "name": inv.email, "email": inv.email,
                    "status": f"Invited {inv.invited_at.strftime('%d %b')} · hasn't joined", "tone": "", "order": 0,
                    "can_remind": not _recent(inv.reminded_at or inv.invited_at),
                    "reminded": _recent(inv.reminded_at)})
    out.sort(key=lambda r: (r["order"], r["name"].lower()))
    return out


def summary(table: list[dict]) -> dict:
    return {
        "licensed": sum(1 for r in table if r["tone"] == "ok"),
        "in_progress": sum(1 for r in table if r["key"].startswith("u:") and r["tone"] != "ok"),
        "invited": sum(1 for r in table if r["key"].startswith("i:")),
        "remindable": sum(1 for r in table if r["can_remind"]),
    }


def remind(org: Organization, key: str, admin: AIDLUser) -> bool:
    """Re-send one person's current email. True = sent (False for an unknown key)."""
    from django.core.exceptions import ValidationError

    try:
        return _remind(org, key, admin)
    except (ValidationError, ValueError, TypeError):  # a made-up id (not a database id)
        return False


def _remind(org: Organization, key: str, admin: AIDLUser) -> bool:
    now = timezone.now()
    kind, _, ident = key.partition(":")
    if kind == "i":
        inv = WorkspaceInvite.objects.filter(pk=ident, organization_id=str(org.pk)).first() if ident else None
        if inv is None or _recent(inv.reminded_at or inv.invited_at):
            return False
        if not mail.invite(inv.email, admin, org):
            return False
        inv.reminded_at = now
        inv.save(update_fields=["reminded_at"])
        return True
    if kind != "u" or not ident:
        return False
    from api.licensing import learners

    user = learners(org).filter(pk=ident).first()
    step = mail.step_of(user, org) if user else ""
    if not step or step == "licence":
        return False
    row, _ = WorkspaceMember.objects.get_or_create(user_id=str(user.pk), defaults={"organization_id": str(org.pk)})
    if _recent(row.reminded_at):
        return False
    if not mail.learner_step(user, org, step):
        return False
    row.reminded_at, row.last_step, row.last_sent_at = now, step, now
    row.save(update_fields=["reminded_at", "last_step", "last_sent_at", "updated_at"])
    return True


def remind_all(org: Organization, admin: AIDLUser) -> int:
    return sum(1 for r in rows(org) if r["can_remind"] and remind(org, r["key"], admin))


def record_invite(org: Organization, email: str, admin: AIDLUser) -> None:
    now = timezone.now()
    inv = WorkspaceInvite.objects.filter(organization_id=str(org.pk), email__iexact=email).first()
    if inv:
        inv.invited_at, inv.reminded_at, inv.invited_by_id = now, None, str(admin.pk)
        inv.save(update_fields=["invited_at", "reminded_at", "invited_by_id"])
    else:
        WorkspaceInvite.objects.create(organization_id=str(org.pk), email=email.lower(),
                                       invited_by_id=str(admin.pk), invited_at=now)
