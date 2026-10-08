"""Admins removing admins and team members (Slack Admin Center).

Rules:
- Only the organization's first admin (the one who installed AIDL) removes
  admins. They can't remove themselves, so there's always one admin left.
- A removed admin loses Admin Center access. If they're still in the AIDL
  channel they become a team member when the plan has a free seat;
  otherwise AIDL takes them out of the channel.
- Any admin can remove a team member: they're taken out of the AIDL channel
  and offboarded (licence suspended, cards stopped; a licence they earned
  keeps counting toward the plan — see plans.py).
"""

from __future__ import annotations

from django.utils import timezone

from . import slack_client
from .models import AIDLUser, Organization
from .plans import seat_holders, user_limit


def _slack_id(member: AIDLUser) -> str:
    return member.microsoft_id.split(":")[-1] if member.microsoft_id.startswith("slack:") else ""


def _dm(token: str, org: Organization, slack_user_id: str, text: str) -> None:
    slack_client.send_dm(token, org, slack_user_id, text=text,
                         blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])


def is_primary_admin(org: Organization, member: AIDLUser) -> bool:
    from .slack_onboarding import primary_admin

    first = primary_admin(org)
    return first is not None and first.pk == member.pk


def _target(org: Organization, user_id: str) -> AIDLUser | None:
    return AIDLUser.objects.filter(pk=user_id, organization_id=str(org.pk), is_active=True).first()


def remove_admin(org: Organization, by: AIDLUser, user_id: str) -> tuple[bool, str]:
    """Returns (done, message for the admin who clicked)."""
    if not is_primary_admin(org, by):
        return False, "Only the admin who set up AIDL can remove admins."
    target = _target(org, user_id)
    if target is None or target.role != AIDLUser.Role.ADMIN:
        return False, "That person isn't an admin any more."
    if target.pk == by.pk or is_primary_admin(org, target):
        return False, "The admin who set up AIDL can't be removed."

    name = target.full_name or target.email
    target.role = AIDLUser.Role.LEARNER
    target.perm_approve_apps = target.perm_access_cards = target.perm_create_card = False
    target.save(update_fields=["role", "perm_approve_apps", "perm_access_cards", "perm_create_card", "updated_at"])

    token = slack_client.bot_token(org)
    slack_id = _slack_id(target)
    notice = f"Your AIDL admin access for *{org.name}* was removed by *{by.full_name or by.email}*."
    if token and slack_id:
        if target.slack_dm_channel_id and target.slack_admin_card_ts:  # replace their Admin Center card
            slack_client.slack_api("chat.update", token, json={
                "channel": target.slack_dm_channel_id, "ts": target.slack_admin_card_ts, "text": notice,
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"🔒 {notice}"}}]})
        else:
            _dm(token, org, slack_id, f"🔒 {notice}")

    # Still in the channel → they're now a team member and need a seat.
    if target.slack_left_at is None and seat_holders(org).count() > user_limit(org):
        if token and slack_id:
            slack_client.slack_api("conversations.kick", token, json={"channel": org.slack_channel_id, "user": slack_id})
        target.slack_left_at = timezone.now()
        target.save(update_fields=["slack_left_at", "updated_at"])
        return True, (f"✓ {name} is no longer an admin. Your plan has no free seat, so they were also "
                      "taken out of the AIDL channel.")
    if token and slack_id and target.slack_left_at is None:
        from .slack_onboarding import onboard_member

        onboard_member(org, slack_id, force=not target.slack_onboarded_at)
    return True, f"✓ {name} is no longer an admin. They're now a team member and use one of your plan's seats."


def remove_member(org: Organization, by: AIDLUser, user_id: str) -> tuple[bool, str]:
    """Take a team member out of AIDL (any admin)."""
    from .slack_onboarding import offboard_member

    if by.role != AIDLUser.Role.ADMIN:
        return False, "Only AIDL admins can remove people."
    target = _target(org, user_id)
    if target is None or target.role == AIDLUser.Role.ADMIN:
        return False, "That person isn't a team member."
    if target.slack_left_at:
        return False, f"{target.full_name or target.email} was already removed."
    token = slack_client.bot_token(org)
    slack_id = _slack_id(target)
    if token and slack_id:
        kicked = slack_client.slack_api("conversations.kick", token, json={"channel": org.slack_channel_id, "user": slack_id})
        if not kicked.get("ok") and kicked.get("error") not in ("not_in_channel", "user_not_found"):
            return False, (f"Slack didn't let AIDL remove them from the channel ({kicked.get('error')}). "
                           "Check Slack's settings for who can remove members from channels.")
        _dm(token, org, slack_id, f"You've been removed from *{org.name}'s* AIDL programme by "
                                  f"*{by.full_name or by.email}*. Your AIDL cards have stopped.")
    offboard_member(org, slack_id)  # same as the member_left_channel event; safe to run twice
    target.refresh_from_db()
    name = target.full_name or target.email
    if target.licence_issued:
        return True, (f"✓ {name} was removed. Their license `{target.licence_number}` is suspended and still "
                      "counts toward your plan. Adding them back to the channel restores it.")
    return True, f"✓ {name} was removed and their seat is free again."
