"""Who a Google Workspace sign-in is in AIDL: the AIDLUser and the Organization.

Rules (same spirit as Slack, Teams and Google Classroom):
  - One Google Workspace domain (company.com, Google's verified "hd" claim) =
    one AIDL organization. The domain → organization link is the same
    GoogleWorkspace row Google Classroom uses, so a company that uses both
    is one organization.
  - Only Google Workspace accounts: a personal Gmail account (no domain) is
    refused, since there's no company to join.
  - The first person of a domain to add AIDL creates the organization and
    becomes its owner admin. Everyone after them joins as a learner (an admin
    can promote them later).
  - Google people are AIDLUser.microsoft_id "google:<google user id>", the
    same id Google Classroom uses, so one Google account is one AIDL person.
  - An existing AIDL account with the same email is reused only when Google
    says the email is verified.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.utils import timezone

from api.models import AIDLUser, Organization
from integrations.google_classroom.models import GoogleWorkspace

from .google_auth import GoogleError, GoogleProfile

PROVIDER = "google_workspace"


def google_id(profile: GoogleProfile) -> str:
    return f"google:{profile.sub}"


def domain_key(domain: str) -> str:
    return f"domain:{domain}"


def sign_in(profile: GoogleProfile) -> tuple[AIDLUser, Organization]:
    """Find or create the person and their company's organization."""
    if not profile.hd:
        raise GoogleError("not_workspace", "Please sign in with your work Google Workspace account. Personal Gmail "
                                           "accounts can't join a company in AIDL.")
    if not profile.email_verified:
        raise GoogleError("email_not_verified", "Google hasn't verified this account's email address yet.")

    user = find_user(profile)
    if user is not None and not user.is_active:
        raise GoogleError("account_disabled", "Your AIDL account is switched off. Ask your AIDL admin.")
    current = _current_org(user) if user is not None else None

    if current is not None:
        # Already in AIDL (e.g. the company started on Slack or Teams): stay in that organization.
        created = False
        org = current
        mapped = organization_for_domain(profile.hd)
        if mapped is None and user.role == AIDLUser.Role.ADMIN:
            _link_domain(profile.hd, org)  # from now on, colleagues signing in with Google join it too
        elif mapped is not None and mapped.pk != org.pk:
            raise GoogleError("other_org", "Your AIDL account already belongs to another organization.")
        _refresh_user(user, profile)
    else:
        org, created = _company(profile)
        if user is None:
            user = _new_user(profile, org, admin=created)
        else:
            _refresh_user(user, profile)
            _join(user, org, admin=created)
    if created:
        org.owner_user_id = str(user.pk)
        org.save(update_fields=["owner_user_id", "updated_at"])
    return user, org


def organization_for_domain(domain: str) -> Organization | None:
    ws = GoogleWorkspace.objects.filter(key=domain_key(domain)).first()
    return Organization.objects.filter(pk=ws.organization_id, is_active=True).first() if ws else None


# --- people ---

def find_user(profile: GoogleProfile) -> AIDLUser | None:
    user = AIDLUser.objects.filter(microsoft_id=google_id(profile)).first()
    if user is None and profile.email and profile.email_verified:
        user = AIDLUser.objects.filter(email__iexact=profile.email).order_by("created_at").first()
    return user


def _new_user(profile: GoogleProfile, org: Organization, admin: bool) -> AIDLUser:
    from api.plans import can_join

    if not admin and not can_join(org, None):
        raise GoogleError("no_free_seats", f"{org.name}'s AIDL plan has no free seats. Please tell your AIDL admin.")
    return AIDLUser.objects.create(
        microsoft_id=google_id(profile),
        provider=PROVIDER,
        email=profile.email,
        first_name=profile.first_name[:100],
        last_name=profile.last_name[:100],
        full_name=profile.full_name[:255],
        avatar_url=profile.picture if len(profile.picture) <= 200 else "",
        enroll_as=AIDLUser.EnrollAs.ORGANIZATION,
        role=AIDLUser.Role.ADMIN if admin else AIDLUser.Role.LEARNER,
        organization_id=str(org.pk),
        organization_name=org.name,
        last_login_at=timezone.now(),
        is_active=True,
    )


def _refresh_user(user: AIDLUser, profile: GoogleProfile) -> None:
    """Fill in what's missing from Google; never overwrite what the person set in AIDL."""
    if not user.microsoft_id:
        user.microsoft_id = google_id(profile)
    if not user.full_name and profile.full_name:
        user.full_name = profile.full_name[:255]
        user.first_name, user.last_name = profile.first_name[:100], profile.last_name[:100]
    if not user.email and profile.email:
        user.email = profile.email
    if not user.avatar_url and profile.picture and len(profile.picture) <= 200:
        user.avatar_url = profile.picture
    user.last_login_at = timezone.now()
    user.save(update_fields=["microsoft_id", "full_name", "first_name", "last_name", "email", "avatar_url",
                             "last_login_at", "updated_at"])


def _join(user: AIDLUser, org: Organization, admin: bool) -> None:
    from api.plans import can_join

    if not admin and not can_join(org, user):
        raise GoogleError("no_free_seats", f"{org.name}'s AIDL plan has no free seats. Please tell your AIDL admin.")
    user.organization_id, user.organization_name = str(org.pk), org.name
    user.enroll_as = AIDLUser.EnrollAs.ORGANIZATION
    user.role = AIDLUser.Role.ADMIN if admin else user.role
    user.save(update_fields=["organization_id", "organization_name", "enroll_as", "role", "updated_at"])


# --- the company ---

def _current_org(user: AIDLUser) -> Organization | None:
    if not user.organization_id:
        return None
    return Organization.objects.filter(pk=user.organization_id, is_active=True).first()


def _company(profile: GoogleProfile) -> tuple[Organization, bool]:
    """The domain's organization, creating it on first use. Returns (org, created)."""
    org = organization_for_domain(profile.hd)
    if org is not None:
        return org, False
    org = _new_organization(profile.hd)
    _link_domain(profile.hd, org)
    return org, True


def _link_domain(domain: str, org: Organization) -> None:
    GoogleWorkspace.objects.update_or_create(
        key=domain_key(domain), defaults={"name": org.name, "domain": domain, "organization_id": str(org.pk)})


def _new_organization(domain: str) -> Organization:
    from api.org_service import ensure_default_apps

    name = domain.strip()[:255] or "My company"
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
