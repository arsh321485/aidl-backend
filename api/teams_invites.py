"""Invite a brand-new (same-tenant) user by email into an org's AIDL Team as
a Learner: add them to the Team via Graph (using the inviting admin's stored
refresh token) + email them a sign-in link. No action from them is required
in Teams — Graph membership + a normal Microsoft sign-in is enough for
ensure_organization_for_login to place them in the right org automatically.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .microsoft_auth import (
    add_team_member,
    refresh_graph_token,
    resolve_user_by_email,
    send_mail_via_graph,
)
from .models import AIDLUser, Invitation, Organization


logger = logging.getLogger(__name__)


def _org_pk(org: Organization) -> str:
    return str(org.pk)


def send_user_invite(*, caller: AIDLUser, email: str, full_name: str = "") -> dict:
    """
    caller: the logged-in admin sending the invite (must have organization_id
    + a stored ms_refresh_token from a login since offline_access was added).
    Returns {"ok": True, ...} or {"ok": False, "error": "...", "message": "..."}.
    """
    email = (email or "").strip().lower()
    if not email:
        return {"ok": False, "error": "email_required"}
    if caller.role != AIDLUser.Role.ADMIN:
        return {
            "ok": False,
            "error": "not_admin",
            "message": "Only an existing admin can invite users.",
        }
    if not caller.organization_id:
        return {"ok": False, "error": "no_organization"}

    org = Organization.objects.filter(pk=caller.organization_id).first()
    if org is None:
        return {"ok": False, "error": "org_not_found"}

    if AIDLUser.objects.filter(email__iexact=email, is_active=True).exists():
        return {
            "ok": False,
            "error": "user_already_exists",
            "message": "This person has already signed in to AIDL.",
        }

    token = secrets.token_urlsafe(32)
    expiry_days = int(getattr(settings, "AIDL_INVITE_EXPIRY_DAYS", 14) or 14)
    invitation = Invitation.objects.create(
        token=token,
        email=email,
        full_name=(full_name or "").strip(),
        role=AIDLUser.Role.LEARNER,
        organization_id=_org_pk(org),
        organization_name=org.name,
        invited_by_email=caller.email,
        expires_at=timezone.now() + timedelta(days=expiry_days),
    )

    access_token = _get_caller_access_token(caller)

    graph_result = _add_to_team(access_token=access_token, team_id=caller.teams_team_id, email=email)
    if graph_result.get("ok"):
        invitation.team_member_added = True
    else:
        invitation.error = (graph_result.get("error") or "")[:255]
    invitation.save(update_fields=["team_member_added", "error"])

    email_result = _send_invite_email(invitation, access_token=access_token)

    return {
        "ok": True,
        "invitation_id": str(invitation.pk),
        "team_member_added": invitation.team_member_added,
        "team_member_error": graph_result.get("error") if not graph_result.get("ok") else "",
        "email_sent": email_result.get("ok", False),
        "email_error": email_result.get("error", ""),
    }


def _get_caller_access_token(caller: AIDLUser) -> str:
    """Mint a fresh Graph access token from the admin's stored refresh token.
    Used for both adding the invitee to the Team and sending the invite email
    from the admin's own mailbox, so we only refresh once per invite."""
    if not caller.ms_refresh_token:
        return ""
    token_result = refresh_graph_token(caller.ms_refresh_token)
    access_token = token_result.get("access_token") if isinstance(token_result, dict) else ""
    if not access_token:
        logger.warning(
            "refresh graph token failed for %s: %s",
            caller.email,
            (token_result or {}).get("error_description") or (token_result or {}).get("error"),
        )
        return ""

    # A rotated refresh token comes back on most requests — keep it current
    # so the next invite doesn't fail once the old one expires.
    new_refresh = token_result.get("refresh_token")
    if new_refresh and new_refresh != caller.ms_refresh_token:
        caller.ms_refresh_token = new_refresh
        caller.save(update_fields=["ms_refresh_token", "updated_at"])

    return access_token


def _add_to_team(*, access_token: str, team_id: str, email: str) -> dict:
    if not team_id:
        return {"ok": False, "error": "caller_missing_team_id"}
    if not access_token:
        return {"ok": False, "error": "caller_missing_refresh_token"}

    aad_user = resolve_user_by_email(access_token, email)
    if not aad_user or not aad_user.get("id"):
        return {"ok": False, "error": "user_not_found_in_tenant"}

    return add_team_member(access_token, team_id=team_id, aad_user_id=aad_user["id"])


def _send_invite_email(invitation: Invitation, *, access_token: str) -> dict:
    from urllib.parse import urlsplit

    redirect_uri = getattr(settings, "MS_REDIRECT_URI", "") or ""
    parts = urlsplit(redirect_uri)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "https://aidl-backend.onrender.com"
    # Kicks off the normal Microsoft sign-in flow; ensure_organization_for_login
    # places them in the right org automatically once they're a Team member.
    login_url = f"{origin}/api/auth/teams/login-redirect/"
    name = invitation.full_name or "there"
    org_name = invitation.organization_name or "AIDL"
    subject = f"You've been added to {org_name}'s AI Driving Licence programme"
    body = (
        f"Hi {name},\n\n"
        f"{invitation.invited_by_email or 'An admin'} has added you to {org_name}'s "
        "AIDL (AI Driving Licence) programme in Microsoft Teams.\n\n"
        f"1. Open Microsoft Teams — you should already see the \"AIDL\" team and "
        f"\"{getattr(settings, 'MS_AIDL_CHANNEL_NAME', 'aidl dashboard')}\" channel.\n"
        f"2. Sign in here with your work account to finish setup and open your "
        f"AIDL User Dashboard:\n   {login_url}\n\n"
        "See you there!\n"
    )
    if not access_token:
        return {"ok": False, "error": "caller_missing_refresh_token"}

    result = send_mail_via_graph(
        access_token,
        to_email=invitation.email,
        subject=subject,
        body_text=body,
    )
    if not result.get("ok"):
        logger.warning(
            "invite email send failed for %s: %s",
            invitation.email,
            result.get("detail") or result.get("error"),
        )
    return result
