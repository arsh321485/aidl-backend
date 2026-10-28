"""Admin permissions.

The **master admin** is the admin who connected AIDL (the organization
owner, slack_onboarding.primary_admin). They have every permission, and
nobody can remove them or change their permissions.

Other admins have the permissions they were given:
    approve_apps    change the AI / IT app lists
    access_cards    view and send reference cards
    manage_admins   add, edit and remove admins (not the master admin)
    billing         upgrade the plan
    approve_cards   approve the cards AIDL sends to the team

Admins can't create cards, so there is no "Create Card" permission in
Slack (AIDLUser.perm_create_card is only used by the Teams app).

An admin can only give the permissions they have themselves. Every change
made by someone other than the master admin is reported to them by DM.
"""

from __future__ import annotations

from .models import AIDLUser, Organization

PERMS = (
    ("approve_apps", "perm_approve_apps", "Approve Apps", "change the AI / IT app lists"),
    ("access_cards", "perm_access_cards", "Access Cards", "view and send reference cards"),
    ("manage_admins", "perm_manage_admins", "Manage admins", "add, edit and remove admins"),
    ("billing", "perm_billing", "Manage plan & billing", "upgrade the plan"),
    ("approve_cards", "perm_approve_cards", "Approve cards", "approve the cards sent to the team"),
)
FIELDS = [field for _k, field, _l, _d in PERMS]
MASTER_LABEL = "👑 Master admin"


def is_master(org: Organization | None, user: AIDLUser | None) -> bool:
    from .slack_onboarding import primary_admin

    if org is None or user is None:
        return False
    first = primary_admin(org)
    return first is not None and first.pk == user.pk


def has(org: Organization | None, user: AIDLUser | None, key: str) -> bool:
    if user is None or user.role != AIDLUser.Role.ADMIN:
        return False
    if is_master(org, user):
        return True
    field = next(f for k, f, _l, _d in PERMS if k == key)
    return bool(getattr(user, field, False))


def grantable(org: Organization | None, user: AIDLUser | None) -> list[str]:
    """Permissions this admin may give to others: the ones they have."""
    if user is None or user.role != AIDLUser.Role.ADMIN:
        return []
    if is_master(org, user):  # looked up once, not once per permission
        return [k for k, _f, _l, _d in PERMS]
    return [k for k, f, _l, _d in PERMS if getattr(user, f, False)]


def labels(user: AIDLUser) -> list[str]:
    return [label for _k, field, label, _d in PERMS if getattr(user, field, False)]


def apply(target: AIDLUser, chosen: list[str], allowed: list[str]) -> None:
    """Set the target's permissions to `chosen`, changing only the ones the
    caller is allowed to grant (others keep their current value)."""
    for key, field, _l, _d in PERMS:
        if key in allowed:
            setattr(target, field, key in chosen)


def notify_master(org: Organization, by: AIDLUser, text: str) -> None:
    """Tell the master admin about a change someone else made."""
    from . import slack_client
    from .slack_onboarding import primary_admin

    master = primary_admin(org)
    if master is None or master.pk == by.pk or not master.microsoft_id.startswith("slack:"):
        return
    slack_client.send_dm(slack_client.bot_token(org), org, master.microsoft_id.split(":")[-1], text=text,
                         blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
