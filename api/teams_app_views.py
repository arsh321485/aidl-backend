"""Microsoft Teams app tabs + Adaptive Card JSON APIs."""

import json

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .auth_jwt import create_access_token
from .auth_views import JWTAuthentication
from .models import AIDLUser
from .teams_admin import (
    ADMIN_TABS,
    build_admin_dashboard,
    build_admin_placeholder,
)
from .teams_cards import CARD_BUILDERS, TEAMS_TABS, build_card, org_display_name
from .teams_channel_tabs import ensure_aidl_channel_tabs, is_aidl_dashboard_channel, target_channel_name
from .teams_messaging import send_channel_adaptive_card
from .org_service import replace_welcome_card


def _teams_base_url(request) -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    return request.build_absolute_uri("/api/teams").rstrip("/")


def _request_user_bits(request) -> dict:
    """Resolve signed-in identity from query, JWT, or Mongo AIDLUser."""
    get = getattr(request, "query_params", None) or request.GET
    full_name = (get.get("full_name") or "").strip()
    org_name = (get.get("org_name") or "").strip()
    email = (get.get("email") or "").strip()

    user = getattr(request, "user", None)
    db_user = None
    if user is not None and getattr(user, "is_authenticated", False) and isinstance(user, AIDLUser):
        db_user = user
        full_name = full_name or (user.full_name or "")
        email = email or (user.email or "")
        org_name = org_name or (user.organization_name or "")

    if email and db_user is None:
        try:
            db_user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
            if db_user:
                full_name = full_name or db_user.full_name
                email = email or db_user.email
                org_name = org_name or db_user.organization_name
        except Exception:  # noqa: BLE001
            pass

    return {
        "full_name": full_name,
        "org_name": org_display_name(org_name),
        "email": email,
        "user": db_user,
    }


def _admin_tab_ids() -> set[str]:
    return {tab_id for tab_id, _label, _icon in ADMIN_TABS}


def _admin_tab_context(request, active_tab: str, user_bits: dict) -> dict:
    base = _teams_base_url(request)
    q_parts = []
    if user_bits.get("full_name"):
        from urllib.parse import urlencode

        q = urlencode(
            {
                k: v
                for k, v in {
                    "full_name": user_bits.get("full_name"),
                    "org_name": user_bits.get("org_name"),
                    "email": user_bits.get("email"),
                }.items()
                if v
            }
        )
        q_parts = [q] if q else []

    suffix = f"?{q_parts[0]}" if q_parts else ""
    tabs = []
    for tab_id, label, icon in ADMIN_TABS:
        tabs.append(
            {
                "id": tab_id,
                "label": label,
                "icon": icon,
                "url": f"{base}/tabs/{tab_id}/{suffix}",
                "active": tab_id == active_tab,
            }
        )

    dashboard = build_admin_dashboard(
        full_name=user_bits.get("full_name", ""),
        org_name=user_bits.get("org_name", ""),
        email=user_bits.get("email", ""),
        user=user_bits.get("user"),
    )
    placeholder = build_admin_placeholder(
        active_tab,
        full_name=user_bits.get("full_name", ""),
        org_name=user_bits.get("org_name", ""),
        email=user_bits.get("email", ""),
        user=user_bits.get("user"),
    )
    return {
        "tabs": tabs,
        "active_tab": active_tab,
        "org_name": user_bits.get("org_name") or org_display_name(),
        "teams_base_url": base,
        "dashboard": dashboard,
        "placeholder": placeholder,
        "dashboard_json": json.dumps(dashboard),
        "placeholder_json": json.dumps(placeholder),
        "next_tab_url": f"{base}/tabs/add-admin/{suffix}",
    }


@xframe_options_exempt
def teams_tab_page(request, tab: str):
    """
    Option C: Admin Center in-page UI (Home + clickable pills).
    Channel login/redirect flow unchanged — only the Home tab content.
    """
    if tab in _admin_tab_ids():
        user_bits = _request_user_bits(request)
        context = _admin_tab_context(request, tab, user_bits)
        return render(request, "teams/admin.html", context)

    # Legacy learner Adaptive Card pages (still available if channel tabs point here)
    if tab not in CARD_BUILDERS:
        raise Http404("Unknown tab")
    from .teams_cards import TEAMS_TABS as LEARNER_TABS

    base = _teams_base_url(request)
    tabs = [
        {
            "id": tab_id,
            "label": label,
            "icon": icon,
            "url": f"{base}/tabs/{tab_id}/",
            "active": tab_id == tab,
        }
        for tab_id, label, icon in LEARNER_TABS
    ]
    context = {
        "tabs": tabs,
        "active_tab": tab,
        "org_name": org_display_name(),
        "teams_base_url": base,
        "card_json": json.dumps(build_card(tab) or {}),
    }
    return render(request, "teams/tab.html", context)


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_admin_json(request, tab: str):
    """Dynamic Admin Center JSON — user name from query / Teams / DB."""
    if tab not in _admin_tab_ids():
        return Response({"error": "unknown_tab"}, status=status.HTTP_404_NOT_FOUND)
    user_bits = _request_user_bits(request)
    if tab == "home":
        return Response(
            {
                "tab": tab,
                "dashboard": build_admin_dashboard(
                    full_name=user_bits["full_name"],
                    org_name=user_bits["org_name"],
                    email=user_bits["email"],
                    user=user_bits.get("user"),
                ),
            }
        )
    return Response(
        {
            "tab": tab,
            "placeholder": build_admin_placeholder(
                tab,
                full_name=user_bits["full_name"],
                org_name=user_bits["org_name"],
                email=user_bits["email"],
                user=user_bits.get("user"),
            ),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_card_json(request, tab: str):
    """Return Adaptive Card JSON for a tab (used by Teams tabs / integrations)."""
    card = build_card(
        tab,
        full_name=request.query_params.get("full_name", ""),
        org_name=request.query_params.get("org_name", ""),
    )
    if card is None:
        return Response({"error": "unknown_tab"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"tab": tab, "card": card})


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_app_index(request):
    """Teams app metadata + Admin Center tab endpoints."""
    base = _teams_base_url(request)
    return Response(
        {
            "app": "AIDL Teams Admin Center",
            "org_display_name": org_display_name(),
            "admin_tabs": [
                {
                    "id": tab_id,
                    "name": label,
                    "tab_url": request.build_absolute_uri(f"/api/teams/tabs/{tab_id}/"),
                    "admin_json": request.build_absolute_uri(
                        f"/api/teams/admin/{tab_id}/"
                    ),
                }
                for tab_id, label, _icon in ADMIN_TABS
            ],
            "legacy_learner_tabs": [
                {
                    "id": tab_id,
                    "name": label,
                    "tab_url": request.build_absolute_uri(f"/api/teams/tabs/{tab_id}/"),
                    "card_url": request.build_absolute_uri(
                        f"/api/teams/cards/{tab_id}/"
                    ),
                }
                for tab_id, label, _icon in TEAMS_TABS
            ],
            "manifest_path": "/teams/manifest.json",
            "send_welcome_on_signup": bool(getattr(settings, "MS_SEND_WELCOME_CARD", True)),
            "teams_base_url": base,
            "note": (
                "Option C: Home tab shows Admin Center UI with in-page pills. "
                "Login/channel create flow unchanged."
            ),
        }
    )


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_install_channel_tabs(request):
    """
    Install Home / Learner's Permit / Highway Code / Traffic Light Check tabs
    on the aidl dashboard channel only (never General).
    """
    user = request.user
    team_id = getattr(user, "teams_team_id", "") or ""
    channel_id = getattr(user, "teams_channel_id", "") or ""
    channel_name = getattr(user, "teams_channel_name", "") or target_channel_name()

    if not team_id or not channel_id:
        return Response(
            {
                "error": "channel_not_configured",
                "message": "Log in via Teams first so aidl dashboard ids are saved.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not is_aidl_dashboard_channel(channel_name):
        return Response(
            {
                "error": "wrong_channel",
                "message": (
                    f"Tabs only install on '{target_channel_name()}' channel, "
                    f"not '{channel_name}'."
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    ms_token = (request.data.get("ms_access_token") or "").strip()
    if not ms_token:
        return Response(
            {"error": "ms_access_token_required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    result = ensure_aidl_channel_tabs(
        ms_token,
        team_id=team_id,
        channel_id=channel_id,
        channel_name=channel_name,
    )
    if not result or result.get("skipped"):
        return Response(
            {"error": "tab_install_skipped", "details": result},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not result.get("ok"):
        return Response(
            {"error": "tab_install_partial", "details": result},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response({"ok": True, "details": result})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_send_welcome(request):
    """
    Manually post the Home welcome Adaptive Card to the user's AIDL channel.
    Requires a valid AIDL JWT and stored team/channel ids from login.
    """
    user = request.user
    team_id = getattr(user, "teams_team_id", "") or ""
    channel_id = getattr(user, "teams_channel_id", "") or ""
    if not team_id or not channel_id:
        return Response(
            {
                "error": "channel_not_configured",
                "message": "Log in via Teams first so AIDL channel ids are saved.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    ms_token = (request.data.get("ms_access_token") or "").strip()
    if not ms_token:
        return Response(
            {
                "error": "ms_access_token_required",
                "message": "Pass Microsoft Graph access token in POST body.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    result = replace_welcome_card(
        ms_token,
        team_id=team_id,
        channel_id=channel_id,
        full_name=user.full_name,
        org_name=user.organization_name or org_display_name(),
        email=user.email,
        user=user,
    )
    if not result or not result.get("ok"):
        return Response(
            {
                "error": "send_failed",
                "message": "Could not post Admin Center card to channel Posts.",
                "details": result,
                "hint": (
                    "Ensure Azure delegated permission ChannelMessage.Send "
                    "has admin consent, then re-login."
                ),
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(
        {
            "ok": True,
            "message_id": result.get("message_id")
            or (result.get("message") or {}).get("id"),
            "tab": "home",
            "card_variant": result.get("card_variant"),
            "channel": {
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": getattr(user, "teams_channel_name", "") or "aidl dashboard",
            },
        }
    )


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_send_card(request, tab: str):
    """Post any tab Adaptive Card to the user's AIDL channel."""
    card = build_card(tab, full_name=request.user.full_name, org_name=org_display_name())
    if card is None:
        return Response({"error": "unknown_tab"}, status=status.HTTP_404_NOT_FOUND)

    team_id = getattr(request.user, "teams_team_id", "") or ""
    channel_id = getattr(request.user, "teams_channel_id", "") or ""
    ms_token = (request.data.get("ms_access_token") or "").strip()
    if not team_id or not channel_id or not ms_token:
        return Response(
            {"error": "missing_channel_or_token"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    result = send_channel_adaptive_card(
        ms_token,
        team_id=team_id,
        channel_id=channel_id,
        card=card,
    )
    if not result or not result.get("ok"):
        return Response(
            {"error": "send_failed", "details": result},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response(
        {
            "ok": True,
            "tab": tab,
            "message_id": result.get("message_id")
            or (result.get("message") or {}).get("id"),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_admin_export_csv(request):
    """Export coverage CSV for the signed-in user's organisation."""
    import csv
    from io import StringIO

    from django.http import HttpResponse

    from .org_service import coverage_csv_rows

    bits = _request_user_bits(request)
    rows = coverage_csv_rows(email=bits.get("email", ""), user=bits.get("user"))
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "full_name",
            "email",
            "role",
            "licence_issued",
            "aup_signed",
            "organization",
            "teams_channel",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="aidl-coverage.csv"'
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def teams_admin_session(request):
    """
    Mint a short-lived AIDL session token for an org admin who is already
    inside the Teams tab (the "Send Admin Invite" flow has no browser-based
    SPA login to hand it a token from). The Teams tab resolves the caller's
    email from the Teams SSO context client-side and posts it here.

    This does not grant admin — it only authenticates a request for someone
    the DB already has on record as role=admin (set the normal way, via a
    prior invite or org setup), the same requirement teams_admin_invite
    enforces. An unrecognised or non-admin email gets nothing.
    """
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response({"error": "email_required"}, status=status.HTTP_400_BAD_REQUEST)
    caller = AIDLUser.objects.filter(
        email__iexact=email, is_active=True, role=AIDLUser.Role.ADMIN
    ).first()
    if caller is None:
        return Response({"error": "not_admin"}, status=status.HTTP_403_FORBIDDEN)
    return Response({"ok": True, "access_token": create_access_token(caller)})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_invite(request):
    """Promote / add an admin by email within the caller's organisation."""
    caller = request.user
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response({"error": "email_required"}, status=status.HTTP_400_BAD_REQUEST)
    if not caller.organization_id:
        return Response(
            {"error": "no_organization"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if caller.role != AIDLUser.Role.ADMIN:
        return Response(
            {
                "error": "not_admin",
                "message": "Only an existing admin can add another admin.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    target = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
    if target is None:
        return Response(
            {
                "error": "user_not_found",
                "message": "User must sign in via Teams once before becoming admin.",
            },
            status=status.HTTP_404_NOT_FOUND,
        )
    admins = AIDLUser.objects.filter(
        organization_id=caller.organization_id,
        role=AIDLUser.Role.ADMIN,
        is_active=True,
    ).count()
    from .models import Organization

    org = Organization.objects.filter(pk=caller.organization_id).first()
    limit = org.admin_seat_limit if org else 3
    if target.role != AIDLUser.Role.ADMIN and admins >= limit:
        return Response(
            {"error": "admin_seats_full", "limit": limit},
            status=status.HTTP_400_BAD_REQUEST,
        )
    target.organization_id = caller.organization_id
    target.organization_name = caller.organization_name or (org.name if org else "")
    target.role = AIDLUser.Role.ADMIN
    target.save(
        update_fields=["organization_id", "organization_name", "role", "updated_at"]
    )
    return Response(
        {
            "ok": True,
            "admin": {
                "email": target.email,
                "full_name": target.full_name,
                "role": target.role,
            },
        }
    )


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_sign_aup(request):
    """Mark Acceptable Use Policy signed for the current user."""
    from django.utils import timezone

    user = request.user
    user.aup_signed = True
    user.aup_signed_at = timezone.now()
    user.save(update_fields=["aup_signed", "aup_signed_at", "updated_at"])
    return Response({"ok": True, "aup_signed": True, "aup_signed_at": user.aup_signed_at})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_issue_licence(request):
    """Issue licence flag for a member in the same organisation."""
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response({"error": "email_required"}, status=status.HTTP_400_BAD_REQUEST)
    caller = request.user
    target = AIDLUser.objects.filter(
        email__iexact=email,
        organization_id=caller.organization_id,
        is_active=True,
    ).first()
    if target is None:
        return Response({"error": "user_not_found"}, status=status.HTTP_404_NOT_FOUND)
    target.licence_issued = True
    target.save(update_fields=["licence_issued", "updated_at"])
    return Response({"ok": True, "email": target.email, "licence_issued": True})
