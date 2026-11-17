"""Create the AIDL user and organization for a Google sign-in."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from api.models import AIDLUser, Invitation, Organization
from api.org_service import ensure_organization_for_login, get_organization_for_user

from . import client
from .cards import admin_home_message
from .models import GoogleChatInstall

logger = logging.getLogger(__name__)


def _org_for_install(install: GoogleChatInstall | None) -> Organization | None:
    if install is None:
        return None
    return Organization.objects.filter(pk=install.organization_id, is_active=True).first()


def already_set_up(profile: dict) -> str:
    """Empty when this person may connect the workspace. Otherwise the reason to stop."""
    domain = profile.get("hosted_domain") or ""
    org = _org_for_install(GoogleChatInstall.objects.filter(hosted_domain=domain).first()) if domain else None
    if org is None:
        return ""
    members = AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True)
    if not members.filter(role=AIDLUser.Role.ADMIN).exists():
        return ""
    me = members.filter(microsoft_id=profile["google_id"]).first()
    if me is None and profile.get("email") and profile.get("email_verified"):
        me = members.filter(email__iexact=profile["email"]).first()
    if me is not None and me.role == AIDLUser.Role.ADMIN:
        return ""
    if me is not None:
        return (f"You're already part of {org.name}'s AIDL programme, so there is nothing to set up here. "
                "Your cards arrive in Google Chat.")
    return (f"AIDL is already set up for {org.name}. Ask your AIDL admin to add you from the Admin Center "
            "in Google Chat. You don't need to sign up here.")


def upsert_user(profile: dict, enroll_as: str) -> AIDLUser | None:
    """Find by Google id, else by a verified email, else create the account."""
    user = AIDLUser.objects.filter(microsoft_id=profile["google_id"]).first()
    if user is None and profile.get("email") and profile.get("email_verified"):
        user = AIDLUser.objects.filter(email__iexact=profile["email"]).first()
    if user is not None:
        if not user.is_active:
            return None
        user.last_login_at = timezone.now()
        if not user.avatar_url and profile.get("avatar_url"):
            user.avatar_url = profile["avatar_url"]
        user.save(update_fields=["last_login_at", "avatar_url", "updated_at"])
        return user

    is_org = enroll_as == AIDLUser.EnrollAs.ORGANIZATION
    full_name = profile.get("full_name") or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    return AIDLUser.objects.create(
        microsoft_id=profile["google_id"],
        provider="google_chat",
        email=profile.get("email") or "",
        first_name=profile.get("first_name") or "",
        last_name=profile.get("last_name") or "",
        full_name=full_name,
        avatar_url=profile.get("avatar_url") or "",
        enroll_as=enroll_as,
        role=AIDLUser.Role.ADMIN if is_org else AIDLUser.Role.LEARNER,
        organization_name=(profile.get("hosted_domain") or "") if is_org else "",
        last_login_at=timezone.now(),
        is_active=True,
    )


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "org"


def _new_organization(name: str) -> Organization:
    base = _slug(name)
    slug, n = base, 1
    while Organization.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return Organization.objects.create(
        name=name,
        slug=slug,
        seats_purchased=int(getattr(settings, "AIDL_DEFAULT_SEATS", 50) or 50),
        admin_seat_limit=int(getattr(settings, "AIDL_DEFAULT_ADMIN_SEATS", 3) or 3),
    )


def ensure_organization(user: AIDLUser, profile: dict) -> Organization:
    """One Workspace domain maps to one organization. A website org with no Google install is reused."""
    domain = profile.get("hosted_domain") or ""
    name = domain or user.organization_name or "My organization"
    org = _org_for_install(GoogleChatInstall.objects.filter(hosted_domain=domain).first()) if domain else None
    if org is None:
        current = get_organization_for_user(user) if user.organization_id else None
        taken = GoogleChatInstall.objects.filter(organization_id=str(current.pk)).first() if current is not None else None
        if current is not None and taken is None:
            org = current
        else:
            org = _new_organization(name)
    user.organization_id = str(org.pk)
    user.organization_name = org.name
    user.save(update_fields=["organization_id", "organization_name", "updated_at"])
    result = ensure_organization_for_login(user, org_name=org.name)
    if not result.owner_user_id:
        result.owner_user_id = str(user.pk)
        result.save(update_fields=["owner_user_id", "updated_at"])
    return result


def _save_install(org: Organization, profile: dict, token: dict) -> GoogleChatInstall:
    domain = profile["hosted_domain"]
    install = GoogleChatInstall.objects.filter(hosted_domain=domain).first()
    if install is None:
        install = GoogleChatInstall.objects.filter(organization_id=str(org.pk)).first()
    if install is None:
        install = GoogleChatInstall(hosted_domain=domain, organization_id=str(org.pk))
    install.hosted_domain = domain
    install.organization_id = str(org.pk)
    install.admin_google_sub = profile.get("sub") or ""
    install.access_token = client.encrypt_token(token.get("access_token") or "")
    if token.get("refresh_token"):
        install.refresh_token = client.encrypt_token(token["refresh_token"])
    expires = int(token.get("expires_in") or 3600)
    install.token_expires_at = timezone.now() + timedelta(seconds=max(expires - 60, 30))
    install.save()
    return install


def setup_workspace(org: Organization, user: AIDLUser, token: dict, profile: dict) -> str:
    """Store the install, open the AIDL space, and post the Admin Center home card.

    Returns the space link. An empty string means the space was not created;
    the admin is still logged in.
    """
    install = _save_install(org, profile, token)
    access = token.get("access_token") or ""
    if not client.ensure_space(install, access):
        return ""
    try:
        card, text = admin_home_message(user)
        client.post_admin_card(install, access, card, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("google chat admin card failed: %s", exc)
    return install.space_url or ""


def accept_pending_invitation(user: AIDLUser) -> None:
    if not user.email:
        return
    try:
        Invitation.objects.filter(
            email__iexact=user.email, status=Invitation.Status.PENDING
        ).update(status=Invitation.Status.ACCEPTED, accepted_at=timezone.now())
    except Exception as exc:  # noqa: BLE001
        logger.warning("accept invitation failed for %s: %s", user.email, exc)
