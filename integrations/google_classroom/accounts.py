"""Who a Google sign-in is in AIDL: the AIDLUser and (for schools) the Organization.

Rules (same spirit as Slack and Moodle):
  - One school = one AIDL organization. A Workspace for Education school is
    found by its Google domain ("hd" claim, which Google verifies), so every
    teacher and student of school.edu lands in the same organization.
  - The first person of a school to add AIDL creates the organization and
    becomes its owner admin. Everyone after them joins as a learner (an
    admin can promote them later).
  - A personal Gmail account (no domain) that adds AIDL gets its own
    organization; nobody joins it by domain.
  - Google people reuse AIDLUser.microsoft_id as "google:<google user id>",
    like Slack's "slack:<team>:<user>".
  - An existing AIDL account (website / Teams / Slack) with the same email
    is reused only when Google says the email is verified.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.utils import timezone

from api.models import AIDLUser, Organization

from .google_oauth import GoogleError, GoogleProfile
from .models import GoogleWorkspace

PROVIDER = "google_classroom"


def google_id(profile: GoogleProfile) -> str:
    return f"google:{profile.sub}"


def school_key(profile: GoogleProfile) -> str:
    return f"domain:{profile.hd}" if profile.hd else f"user:{profile.sub}"


def sign_in(profile: GoogleProfile, enroll_as: str) -> tuple[AIDLUser, Organization | None]:
    """Find or create the person. enroll_as="organization" = "Add to Google Classroom"
    (sets up or joins the school); "individual" = a personal AIDL account."""
    user = find_user(profile)
    if user is not None and not user.is_active:
        raise GoogleError("account_disabled", "Your AIDL account is switched off. Ask your AIDL admin.")
    wants_org = enroll_as == AIDLUser.EnrollAs.ORGANIZATION

    if user is None:
        if not wants_org:
            return _new_user(profile, org=None, admin=False), None
        org, created = _school(profile)
        user = _new_user(profile, org, admin=created)
        if created:
            _set_owner(org, user)
        return user, org

    _refresh_user(user, profile)
    org = _current_org(user)
    if wants_org and org is None:
        org, created = _school(profile)
        _join(user, org, admin=created)
        if created:
            _set_owner(org, user)
    elif wants_org and user.role == AIDLUser.Role.ADMIN:
        _link_school(profile, org)
    return user, org


# --- people ---

def find_user(profile: GoogleProfile) -> AIDLUser | None:
    user = AIDLUser.objects.filter(microsoft_id=google_id(profile)).first()
    if user is None and profile.email and profile.email_verified:
        user = AIDLUser.objects.filter(email__iexact=profile.email).order_by("created_at").first()
    return user


def _new_user(profile: GoogleProfile, org: Organization | None, admin: bool) -> AIDLUser:
    from api.plans import can_join

    if org is not None and not admin and not can_join(org, None):
        raise GoogleError("no_free_seats", f"{org.name}'s AIDL plan has no free seats. Please tell your teacher.")
    return AIDLUser.objects.create(
        microsoft_id=google_id(profile),
        provider=PROVIDER,
        email=profile.email,
        first_name=profile.first_name[:100],
        last_name=profile.last_name[:100],
        full_name=profile.full_name[:255],
        avatar_url=profile.picture if len(profile.picture) <= 200 else "",
        enroll_as=AIDLUser.EnrollAs.ORGANIZATION if org is not None else AIDLUser.EnrollAs.INDIVIDUAL,
        role=AIDLUser.Role.ADMIN if admin else AIDLUser.Role.LEARNER,
        organization_id=str(org.pk) if org is not None else "",
        organization_name=org.name if org is not None else "",
        last_login_at=timezone.now(),
        is_active=True,
    )


def _refresh_user(user: AIDLUser, profile: GoogleProfile) -> None:
    """Fill in what's missing from Google; never overwrite what the person set in AIDL."""
    if not user.full_name and profile.full_name:
        user.full_name = profile.full_name[:255]
        user.first_name, user.last_name = profile.first_name[:100], profile.last_name[:100]
    if not user.email and profile.email:
        user.email = profile.email
    if not user.avatar_url and profile.picture and len(profile.picture) <= 200:
        user.avatar_url = profile.picture
    user.last_login_at = timezone.now()
    user.save(update_fields=["full_name", "first_name", "last_name", "email", "avatar_url", "last_login_at",
                             "updated_at"])


def _join(user: AIDLUser, org: Organization, admin: bool) -> None:
    from api.plans import can_join

    if not admin and not can_join(org, user):
        raise GoogleError("no_free_seats", f"{org.name}'s AIDL plan has no free seats. Please tell your teacher.")
    user.organization_id, user.organization_name = str(org.pk), org.name
    user.enroll_as = AIDLUser.EnrollAs.ORGANIZATION
    if admin:
        user.role = AIDLUser.Role.ADMIN
    user.save(update_fields=["organization_id", "organization_name", "enroll_as", "role", "updated_at"])


# --- schools ---

def _current_org(user: AIDLUser) -> Organization | None:
    if not user.organization_id:
        return None
    return Organization.objects.filter(pk=user.organization_id, is_active=True).first()


def _school(profile: GoogleProfile) -> tuple[Organization, bool]:
    """The school's organization, creating it on first use. Returns (org, created)."""
    key = school_key(profile)
    ws = GoogleWorkspace.objects.filter(key=key).first()
    if ws is not None:
        org = Organization.objects.filter(pk=ws.organization_id, is_active=True).first()
        if org is not None:
            return org, False
    name = profile.hd or (f"{profile.full_name}'s classroom" if profile.full_name else "My classroom")
    org = _new_organization(name)
    GoogleWorkspace.objects.update_or_create(
        key=key, defaults={"name": org.name, "domain": profile.hd, "organization_id": str(org.pk)})
    return org, True


def _link_school(profile: GoogleProfile, org: Organization | None) -> None:
    """An admin whose organization started on the website / Slack / Teams adds
    Google Classroom: their school's domain now points at that organization."""
    if org is None or GoogleWorkspace.objects.filter(key=school_key(profile)).exists():
        return
    GoogleWorkspace.objects.create(key=school_key(profile), name=org.name, domain=profile.hd,
                                   organization_id=str(org.pk))


def _set_owner(org: Organization, user: AIDLUser) -> None:
    org.owner_user_id = str(user.pk)
    org.save(update_fields=["owner_user_id", "updated_at"])


def _new_organization(name: str) -> Organization:
    from api.org_service import ensure_default_apps

    name = (name or "My school").strip()[:255]
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"
    slug, n = base, 1
    while Organization.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    org = Organization.objects.create(
        name=name,
        slug=slug,
        seats_purchased=int(getattr(settings, "AIDL_DEFAULT_SEATS", 50) or 50),
        admin_seat_limit=int(getattr(settings, "AIDL_DEFAULT_ADMIN_SEATS", 3) or 3),
    )
    ensure_default_apps(org)  # the starting AI / IT app registry, as for Slack, Teams and Moodle
    return org
