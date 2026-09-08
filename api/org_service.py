"""Ensure organisation + seed registry apps; compute Admin Center from DB."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from .models import AIDLUser, Organization, RegisteredApp
from .teams_cards import first_name, logo_url, org_display_name, policy_url


logger = logging.getLogger(__name__)

DEFAULT_AI_APPS = (
    "ChatGPT Enterprise",
    "Microsoft Copilot",
    "GitHub Copilot",
    "Claude for Work",
    "Gemini for Google Workspace",
    "Custom RAG Assistant",
)
DEFAULT_IT_APPS = (
    "Microsoft 365",
    "Azure AD / Entra ID",
    "ServiceNow",
    "Jira",
    "Confluence",
    "Slack",
)


def _slugify(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return value or "org"


def _org_id(org: Organization) -> str:
    return str(org.pk)


def get_organization_for_user(user: AIDLUser | None = None, *, org_name: str = "") -> Organization | None:
    if user and getattr(user, "organization_id", ""):
        try:
            return Organization.objects.filter(pk=user.organization_id, is_active=True).first()
        except Exception:  # noqa: BLE001
            pass
    if user and getattr(user, "teams_team_id", ""):
        org = Organization.objects.filter(
            teams_team_id=user.teams_team_id,
            is_active=True,
        ).first()
        if org:
            return org
    name = (org_name or getattr(user, "organization_name", "") or "").strip()
    if name:
        return Organization.objects.filter(name__iexact=name, is_active=True).first()
    return None


def ensure_default_apps(org: Organization) -> None:
    org_id = _org_id(org)
    existing = {
        (a.name, a.app_type)
        for a in RegisteredApp.objects.filter(organization_id=org_id, is_active=True)
    }
    to_create = []
    for idx, name in enumerate(DEFAULT_AI_APPS):
        key = (name, RegisteredApp.AppType.AI)
        if key in existing:
            continue
        status = (
            RegisteredApp.Status.APPROVED
            if idx < 4
            else RegisteredApp.Status.PENDING
        )
        to_create.append(
            RegisteredApp(
                organization_id=org_id,
                name=name,
                app_type=RegisteredApp.AppType.AI,
                status=status,
            )
        )
    for idx, name in enumerate(DEFAULT_IT_APPS):
        key = (name, RegisteredApp.AppType.IT)
        if key in existing:
            continue
        status = (
            RegisteredApp.Status.APPROVED
            if idx < 5
            else RegisteredApp.Status.PENDING
        )
        to_create.append(
            RegisteredApp(
                organization_id=org_id,
                name=name,
                app_type=RegisteredApp.AppType.IT,
                status=status,
            )
        )
    if to_create:
        RegisteredApp.objects.bulk_create(to_create)


def ensure_organization_for_login(
    user: AIDLUser,
    *,
    channel_info: dict | None = None,
    org_name: str = "",
) -> Organization:
    """
    Create/link Organisation on Teams login and mark user as admin when
    enroll_as=organization (or first member of the org).
    """
    channel_info = channel_info or {}
    team_id = (channel_info.get("team_id") or user.teams_team_id or "").strip()
    name = (
        (org_name or "").strip()
        or (user.organization_name or "").strip()
        or org_display_name()
    )

    org = None
    if team_id:
        org = Organization.objects.filter(teams_team_id=team_id, is_active=True).first()
    if org is None and user.organization_id:
        org = Organization.objects.filter(pk=user.organization_id, is_active=True).first()
    if org is None:
        org = Organization.objects.filter(name__iexact=name, is_active=True).first()

    if org is None:
        renew = date.today().replace(month=3, day=1)
        if renew <= date.today():
            renew = renew.replace(year=renew.year + 1)
        slug = _slugify(name)
        base_slug = slug
        n = 1
        while Organization.objects.filter(slug=slug).exists():
            n += 1
            slug = f"{base_slug}-{n}"
        org = Organization.objects.create(
            name=name,
            slug=slug,
            teams_team_id=team_id,
            seats_purchased=int(getattr(settings, "AIDL_DEFAULT_SEATS", 50) or 50),
            seats_renews_on=renew,
            admin_seat_limit=int(getattr(settings, "AIDL_DEFAULT_ADMIN_SEATS", 3) or 3),
            rollout_steps_done=3,
            rollout_steps_total=4,
            policy_url=policy_url(),
        )
    else:
        changed = False
        if team_id and org.teams_team_id != team_id:
            org.teams_team_id = team_id
            changed = True
        if changed:
            org.save(update_fields=["teams_team_id", "updated_at"])

    ensure_default_apps(org)

    org_pk = _org_id(org)
    role = AIDLUser.Role.ADMIN if user.enroll_as == AIDLUser.EnrollAs.ORGANIZATION else user.role
    # First linked member becomes admin if none exist yet.
    admin_count = AIDLUser.objects.filter(
        organization_id=org_pk,
        role=AIDLUser.Role.ADMIN,
        is_active=True,
    ).count()
    if admin_count == 0:
        role = AIDLUser.Role.ADMIN

    user.organization_id = org_pk
    user.organization_name = org.name
    user.role = role
    user.save(
        update_fields=[
            "organization_id",
            "organization_name",
            "role",
            "updated_at",
        ]
    )
    return org


def _members_qs(org: Organization):
    return AIDLUser.objects.filter(organization_id=_org_id(org), is_active=True)


def compute_org_metrics(org: Organization) -> dict:
    members = list(_members_qs(org))
    enrolled = len(members)
    licences_issued = sum(1 for m in members if m.licence_issued)
    aup_unsigned = sum(1 for m in members if not m.aup_signed)
    admins = [m for m in members if m.role == AIDLUser.Role.ADMIN]
    admin_count = len(admins)

    org_id = _org_id(org)
    apps = list(
        RegisteredApp.objects.filter(organization_id=org_id, is_active=True)
    )
    ai_apps = [a for a in apps if a.app_type == RegisteredApp.AppType.AI]
    it_apps = [a for a in apps if a.app_type == RegisteredApp.AppType.IT]
    approved = [a for a in apps if a.status == RegisteredApp.Status.APPROVED]
    total_apps = len(apps)

    licence_pct = int(round((licences_issued / enrolled) * 100)) if enrolled else 0
    renew = org.seats_renews_on
    renew_text = renew.strftime("%d %b") if renew else "TBD"

    return {
        "seats_purchased": org.seats_purchased,
        "seats_renews_on": renew_text,
        "licences_issued": licences_issued,
        "licence_pct": licence_pct,
        "enrolled": enrolled,
        "aup_unsigned": aup_unsigned,
        "admin_count": admin_count,
        "admin_seat_limit": org.admin_seat_limit,
        "approved_apps": len(approved),
        "total_apps": total_apps,
        "ai_apps": len(ai_apps),
        "it_apps": len(it_apps),
        "rollout_steps_done": org.rollout_steps_done,
        "rollout_steps_total": org.rollout_steps_total,
        "members": members,
        "admins": admins,
        "ai_app_list": ai_apps,
        "it_app_list": it_apps,
        "all_apps": apps,
    }


def build_admin_dashboard_from_db(
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user: AIDLUser | None = None,
) -> dict:
    if user is None and email:
        user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()

    org = get_organization_for_user(user, org_name=org_name)
    if org is None:
        # Soft create so Home never shows empty static demo forever.
        if user is not None:
            org = ensure_organization_for_login(user, org_name=org_name or org_display_name())
        else:
            name = org_display_name(org_name)
            org = Organization.objects.filter(name__iexact=name, is_active=True).first()
            if org is None:
                renew = date.today() + timedelta(days=180)
                org = Organization.objects.create(
                    name=name,
                    slug=_slugify(name),
                    seats_purchased=50,
                    seats_renews_on=renew,
                    policy_url=policy_url(),
                )
            ensure_default_apps(org)

    metrics = compute_org_metrics(org)
    display = (full_name or (user.full_name if user else "") or "").strip() or "there"
    name = first_name(display)
    user_email = (email or (user.email if user else "") or "").strip()

    aup_alert = metrics["aup_unsigned"] > 0
    return {
        "full_name": display,
        "first_name": name,
        "email": user_email,
        "org_name": org.name,
        "organization_id": _org_id(org),
        "role": (user.role if user else "") or "",
        "logo_url": logo_url(),
        "welcome_title": f"👋 Welcome to your Admin Center, {name}",
        "welcome_body": (
            "You provision the seats, set the house rules, and keep everything "
            "running smoothly. Your team does the driving."
        ),
        "stats": [
            {
                "label": "SEATS PURCHASED",
                "value": str(metrics["seats_purchased"]),
                "sub": f"renews {metrics['seats_renews_on']}",
                "alert": False,
            },
            {
                "label": "LICENCES ISSUED",
                "value": str(metrics["licences_issued"]),
                "sub": (
                    f"{metrics['licence_pct']}% of enrolled"
                    if metrics["enrolled"]
                    else "0 enrolled yet"
                ),
                "alert": False,
            },
            {
                "label": "AUP UNSIGNED",
                "value": str(metrics["aup_unsigned"]),
                "sub": "blocks ethics gate",
                "alert": aup_alert,
            },
        ],
        "governance": [
            {
                "label": "TOTAL ADMINS",
                "value": str(metrics["admin_count"]),
                "sub": f"of {metrics['admin_seat_limit']} seats",
            },
            {
                "label": "TOTAL APPROVED",
                "value": str(metrics["approved_apps"]),
                "sub": f"of {metrics['total_apps']} apps",
            },
            {
                "label": "AI APPLICATIONS",
                "value": str(metrics["ai_apps"]),
                "sub": "in registry",
            },
            {
                "label": "IT APPLICATIONS",
                "value": str(metrics["it_apps"]),
                "sub": "in registry",
            },
        ],
        "export_label": "⬇ Export Coverage CSV",
        "export_url": "/api/teams/admin/export/",
        "progress_text": (
            f"{metrics['rollout_steps_done']} of {metrics['rollout_steps_total']} "
            f"rollout steps done · {org.name}"
        ),
        "next_step": "→ Next: bring in another admin to help run this",
        "next_tab": "add-admin",
        "metrics": {
            "enrolled": metrics["enrolled"],
            "licences_issued": metrics["licences_issued"],
            "aup_unsigned": metrics["aup_unsigned"],
            "admin_count": metrics["admin_count"],
        },
    }


def build_admin_tab_payload(
    tab: str,
    *,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user: AIDLUser | None = None,
) -> dict:
    if user is None and email:
        user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
    org = get_organization_for_user(user, org_name=org_name)
    if org is None and user is not None:
        org = ensure_organization_for_login(user, org_name=org_name or org_display_name())
    if org is None:
        return {
            "title": tab,
            "org_name": org_display_name(org_name),
            "items": [],
            "empty": True,
            "message": "Organisation not ready yet. Sign in via Teams first.",
        }

    metrics = compute_org_metrics(org)
    display = (full_name or (user.full_name if user else "") or "").strip()
    name = first_name(display)
    base = {
        "full_name": display,
        "first_name": name,
        "email": (email or (user.email if user else "") or "").strip(),
        "org_name": org.name,
        "organization_id": _org_id(org),
        "logo_url": logo_url(),
        "heading": f"{tab.replace('-', ' ').title()} — {org.name}",
    }

    if tab == "add-admin":
        items = [
            {
                "name": m.full_name or m.email,
                "email": m.email,
                "role": m.role,
                "last_login_at": m.last_login_at.isoformat() if m.last_login_at else "",
            }
            for m in metrics["admins"]
        ]
        return {
            **base,
            "title": "Add Admin",
            "body": (
                f"{metrics['admin_count']} of {metrics['admin_seat_limit']} admin seats in use. "
                "Invite another admin from your organisation."
            ),
            "items": items,
            "admin_seat_limit": metrics["admin_seat_limit"],
            "admin_count": metrics["admin_count"],
            "can_add": metrics["admin_count"] < metrics["admin_seat_limit"],
        }

    if tab == "policy":
        unsigned = [
            {
                "name": m.full_name or m.email,
                "email": m.email,
                "aup_signed": m.aup_signed,
            }
            for m in metrics["members"]
            if not m.aup_signed
        ]
        signed = [m for m in metrics["members"] if m.aup_signed]
        return {
            **base,
            "title": "Policy",
            "body": org.policy_title,
            "policy_url": org.policy_url or policy_url(),
            "signed_count": len(signed),
            "unsigned_count": len(unsigned),
            "items": unsigned,
            "heading": f"Policy — {org.name}",
        }

    if tab == "cards":
        from .card_service import build_cards_payload

        cards = build_cards_payload(org)
        return {
            **base,
            "title": "Cards",
            "body": (
                "Pick the reference cards your team needs — view one, request it, "
                "then send it into the channel where they already work."
            ),
            "heading": f"Send Cards — {org.name}",
            "cards": cards,
            # Kept for the generic placeholder renderer / older API consumers.
            "items": [
                {
                    "name": item["title"],
                    "status": item["status"],
                    "description": item["desc"],
                }
                for item in cards["items"]
            ],
        }

    if tab == "ai-apps":
        items = [
            {
                "name": a.name,
                "status": a.status,
                "description": a.description,
            }
            for a in metrics["ai_app_list"]
        ]
        return {
            **base,
            "title": "AI Apps",
            "body": f"{len(items)} AI applications in registry.",
            "items": items,
            "heading": f"AI Apps — {org.name}",
        }

    if tab == "it-apps":
        items = [
            {
                "name": a.name,
                "status": a.status,
                "description": a.description,
            }
            for a in metrics["it_app_list"]
        ]
        return {
            **base,
            "title": "IT Apps",
            "body": f"{len(items)} IT applications in registry.",
            "items": items,
            "heading": f"IT Apps — {org.name}",
        }

    return {**base, "title": tab, "body": "", "items": []}


def coverage_csv_rows(*, email: str = "", user: AIDLUser | None = None) -> list[dict]:
    if user is None and email:
        user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
    org = get_organization_for_user(user)
    if org is None:
        return []
    rows = []
    for m in _members_qs(org):
        rows.append(
            {
                "full_name": m.full_name,
                "email": m.email,
                "role": m.role,
                "licence_issued": "yes" if m.licence_issued else "no",
                "aup_signed": "yes" if m.aup_signed else "no",
                "organization": org.name,
                "teams_channel": m.teams_channel_name,
            }
        )
    return rows
