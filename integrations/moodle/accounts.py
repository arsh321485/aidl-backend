"""Who a Moodle launch is in AIDL: the AIDLUser and the Organization.

Same rules as Slack (api/auth_views.py):
  - One Moodle site (LtiPlatform) = one AIDL organization.
  - The first teacher / site admin to open AIDL creates the organization and
    becomes its owner admin. Everyone after them joins as a learner (an admin
    can promote them later, as in Slack).
  - Students can't create the organization: a teacher has to open AIDL first.
  - Moodle people reuse AIDLUser.microsoft_id as "moodle:<platform id>:<moodle user id>",
    like Slack's "slack:<team>:<user>".
  - We never link to an existing AIDL account by email: a Moodle site admin
    can type any email for a user, so an email from Moodle proves nothing.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.utils import timezone

from api.models import AIDLUser, Organization
from integrations.lti_core.launch import LaunchData, LtiError

PROVIDER = "moodle"


def moodle_id(launch: LaunchData) -> str:
    return f"moodle:{launch.platform.pk}:{launch.user_id}"


def sign_in(launch: LaunchData) -> tuple[AIDLUser, Organization]:
    """Find or create the person (and on the first teacher launch, the organization)."""
    platform = launch.platform
    org = Organization.objects.filter(pk=platform.organization_id, is_active=True).first() \
        if platform.organization_id else None
    user = AIDLUser.objects.filter(microsoft_id=moodle_id(launch)).first()
    if user is not None and not user.is_active:
        raise LtiError("Your AIDL account is switched off. Ask your AIDL admin.")

    created_org = False
    if org is None:
        if not launch.is_staff:
            raise LtiError("AIDL isn't set up for your school yet. A teacher or the site admin has to open AIDL "
                           "first. Please try again after that.")
        org = _new_organization(launch.platform_name or platform.name)
        platform.organization_id = str(org.pk)
        platform.save(update_fields=["organization_id", "updated_at"])
        created_org = True

    if user is None:
        user = _new_user(launch, org, admin=created_org)
    else:
        _refresh_user(user, launch, org)

    if created_org:
        org.owner_user_id = str(user.pk)
        org.save(update_fields=["owner_user_id", "updated_at"])
    return user, org


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
    ensure_default_apps(org)  # the starting AI / IT app registry, as for Slack and Teams
    return org


def _new_user(launch: LaunchData, org: Organization, admin: bool) -> AIDLUser:
    from api.plans import can_join

    if not admin and not can_join(org, None):
        raise LtiError(f"{org.name}'s AIDL plan has no free seats. Please tell your teacher.")
    return AIDLUser.objects.create(
        microsoft_id=moodle_id(launch),
        provider=PROVIDER,
        email=launch.email,
        first_name=launch.first_name[:100],
        last_name=launch.last_name[:100],
        full_name=launch.full_name[:255],
        enroll_as=AIDLUser.EnrollAs.ORGANIZATION,
        role=AIDLUser.Role.ADMIN if admin else AIDLUser.Role.LEARNER,
        organization_id=str(org.pk),
        organization_name=org.name,
        last_login_at=timezone.now(),
        is_active=True,
    )


def _refresh_user(user: AIDLUser, launch: LaunchData, org: Organization) -> None:
    """Keep the name / email Moodle sends up to date."""
    if user.organization_id not in ("", str(org.pk)):
        raise LtiError("Your account belongs to another AIDL organization.")
    user.organization_id, user.organization_name = str(org.pk), org.name
    if launch.full_name:
        user.full_name = launch.full_name[:255]
        user.first_name, user.last_name = launch.first_name[:100], launch.last_name[:100]
    if launch.email:
        user.email = launch.email
    user.last_login_at = timezone.now()
    user.save(update_fields=["organization_id", "organization_name", "full_name", "first_name", "last_name", "email",
                             "last_login_at", "updated_at"])
