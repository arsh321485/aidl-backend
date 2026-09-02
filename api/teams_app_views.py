"""Microsoft Teams app tabs + Adaptive Card JSON APIs."""

import json

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .auth_views import JWTAuthentication
from .teams_cards import CARD_BUILDERS, TEAMS_TABS, build_card, org_display_name
from .teams_messaging import send_channel_adaptive_card, send_welcome_card_after_signup


def _teams_base_url(request) -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    return request.build_absolute_uri("/api/teams").rstrip("/")


def _tab_context(request, active_tab: str) -> dict:
    base = _teams_base_url(request)
    tabs = []
    for tab_id, label, icon in TEAMS_TABS:
        tabs.append(
            {
                "id": tab_id,
                "label": label,
                "icon": icon,
                "url": f"{base}/tabs/{tab_id}/",
                "active": tab_id == active_tab,
            }
        )
    return {
        "tabs": tabs,
        "active_tab": active_tab,
        "org_name": org_display_name(),
        "teams_base_url": base,
    }


@xframe_options_exempt
def teams_tab_page(request, tab: str):
    """HTML tab page for Teams static tabs (menu bar + card host)."""
    if tab not in CARD_BUILDERS:
        raise Http404("Unknown tab")
    context = _tab_context(request, tab)
    context["card_json"] = json.dumps(build_card(tab) or {})
    return render(request, "teams/tab.html", context)


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
    """Teams app metadata + tab/card endpoints."""
    base = _teams_base_url(request)
    return Response(
        {
            "app": "AIDL Teams",
            "org_display_name": org_display_name(),
            "tabs": [
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
        }
    )


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

    result = send_welcome_card_after_signup(
        ms_token,
        team_id=team_id,
        channel_id=channel_id,
        full_name=user.full_name,
        org_name=org_display_name(),
    )
    if not result:
        return Response(
            {"error": "send_failed", "message": "Could not post welcome card to channel."},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response({"ok": True, "message_id": result.get("id"), "tab": "home"})


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
    if not result:
        return Response({"error": "send_failed"}, status=status.HTTP_502_BAD_GATEWAY)
    return Response({"ok": True, "tab": tab, "message_id": result.get("id")})
