"""What the admin's and the learner's buttons do, shared by the AIDL web page
(views.py) and the Gmail side panel (addon.py). Each returns a notice to show."""

from __future__ import annotations

import json
import re

from api.models import AIDLUser, Organization
from integrations.moodle import flow

from . import mail, progress

MAX_INVITES = 200


def learner(user: AIDLUser, org: Organization, post) -> str:
    """A learner's button (Let's go, I agree, Got it ...): the step, then their next email."""
    notice = flow.learner_action(user, org, post)
    user.refresh_from_db()
    if mail.send_next(user, org):
        notice = notice or "✅ Done. Your next step is in your Gmail."
    return notice


def agree_all(user: AIDLUser, org: Organization, cards: str) -> str:
    """ "I agree to all": every policy card that was on screen, as if each "I agree" was pressed.

    `cards` is JSON [[n, version], ...]. A card whose version changed since is skipped
    (the learner must read the new one), then one "next step" email, not one per card."""
    try:
        shown = [(str(n), str(v)) for n, v in json.loads(cards or "[]")]
    except (ValueError, TypeError):
        shown = []
    agreed, skipped = 0, 0
    for n, version in shown:
        problem = flow.learner_action(user, org, {"action": "agree", "n": n, "version": version})
        if problem:
            skipped += 1
        else:
            agreed += 1
    user.refresh_from_db()
    mail.send_next(user, org)
    if not agreed:
        return "Nothing to agree to. Please press Refresh." if not skipped else \
            "These cards were updated while you were reading. Please read the new versions."
    notice = f"✅ You agreed to {agreed} policy card{'' if agreed == 1 else 's'}."
    if skipped:
        notice += f" {skipped} changed while you were reading: please read and agree to {'it' if skipped == 1 else 'them'}."
    return notice


def setup(user: AIDLUser, org: Organization, post) -> str:
    """An admin's setup button (approve a starter card / start the programme)."""
    from api import admin_setup
    from api.licensing import learners

    was_started = admin_setup.started(org)
    notice = flow.teacher_action(user, org, post)
    if not was_started and admin_setup.started(org):
        mail.started(user, org)
        for person in learners(org):
            mail.send_next(person, org)  # their Welcome email
    return notice


def invite(user: AIDLUser, org: Organization, text: str) -> str:
    """The admin's "Invite your team": one email per colleague (same company domain only)."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    if user.role != AIDLUser.Role.ADMIN:
        return "Only your AIDL admins can invite people."
    domain = user.email.rsplit("@", 1)[-1].lower()
    emails, skipped = [], []
    for raw in re.split(r"[\s,;]+", text or ""):
        email = raw.strip().strip("<>").lower()
        if not email:
            continue
        try:
            validate_email(email)
        except ValidationError:
            skipped.append(email)
            continue
        if email.rsplit("@", 1)[-1] != domain:
            skipped.append(email)
        elif email not in emails and email != user.email.lower():
            emails.append(email)
    sent = 0
    for email in emails[:MAX_INVITES]:
        if mail.invite(email, user, org):
            progress.record_invite(org, email, user)
            sent += 1
    notice = f"✉️ Invited {sent} colleague{'' if sent == 1 else 's'} by email."
    if skipped:
        notice += f" Skipped {len(skipped)}: only @{domain} addresses can join."
    return notice


def remind(user: AIDLUser, org: Organization, who: str) -> str:
    """The Admin Center's "Remind" (one person) and "Remind everyone who hasn't finished"."""
    if user.role != AIDLUser.Role.ADMIN:
        return "Only your AIDL admins can send reminders."
    if who == "all":
        sent = progress.remind_all(org, user)
        return (f"🔔 Reminded {sent} {'person' if sent == 1 else 'people'} by email." if sent
                else "Nobody to remind right now. Each person can be reminded once a day.")
    return ("🔔 Reminder sent." if progress.remind(org, who, user)
            else "Couldn't send that reminder. Each person can be reminded once a day.")
