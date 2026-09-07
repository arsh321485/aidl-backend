"""
Teams Bot messaging endpoint — VaptFix-style in-Posts Adaptive Card navigation.

Azure Bot must call:
  POST https://aidl-backend.onrender.com/api/teams/bot/messages/
"""

from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import AIDLUser
from .teams_adaptive_admin import build_admin_adaptive_card
from .teams_cards import org_display_name


logger = logging.getLogger(__name__)


def _extract_action_data(activity: dict) -> dict:
    value = activity.get("value") or {}
    if isinstance(value, dict):
        # Adaptive Card Action.Execute payload shapes vary by client.
        if "action" in value or "tab" in value:
            return value
        nested = value.get("action") or value.get("data") or {}
        if isinstance(nested, dict):
            if "data" in nested and isinstance(nested["data"], dict):
                return nested["data"]
            return nested
    return {}


def _resolve_user(data: dict, activity: dict) -> tuple[AIDLUser | None, str, str]:
    email = (data.get("email") or "").strip()
    from_user = activity.get("from") or {}
    aad = (from_user.get("aadObjectId") or "").strip()
    name = (from_user.get("name") or "").strip()

    user = None
    if email:
        user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
    if user is None and aad:
        user = AIDLUser.objects.filter(microsoft_id=aad, is_active=True).first()
    if user is not None:
        email = email or user.email
        name = name or user.full_name
    return user, name, email


def _card_for_action(data: dict, activity: dict) -> dict:
    user, full_name, email = _resolve_user(data, activity)
    action = (data.get("action") or "nav").strip().lower()
    tab = (data.get("tab") or "home").strip().lower()
    if action == "export":
        # Stay on home; export is handled via OpenUrl in a follow-up message text.
        tab = "home"
    org_name = ""
    if user is not None:
        org_name = user.organization_name or ""
    return build_admin_adaptive_card(
        tab,
        full_name=full_name,
        org_name=org_name or org_display_name(),
        email=email,
        user=user,
        interactive=True,
    )


@api_view(["POST", "GET"])
@permission_classes([AllowAny])
def teams_bot_messages(request):
    """
    Bot Framework messaging endpoint.
    GET — health/probe for Azure Bot registration.
    POST — activities (message / invoke adaptiveCard/action).
    """
    if request.method == "GET":
        return Response(
            {
                "ok": True,
                "endpoint": "aidl-teams-bot",
                "bot_configured": bool(
                    getattr(settings, "MS_BOT_APP_ID", "")
                    or getattr(settings, "MS_CLIENT_ID", "")
                ),
            }
        )

    activity = request.data if isinstance(request.data, dict) else {}
    activity_type = (activity.get("type") or "").lower()
    logger.info("teams bot activity type=%s name=%s", activity_type, activity.get("name"))

    # Adaptive Card button click → replace card in Posts (no browser tab).
    if activity_type == "invoke" and activity.get("name") in {
        "adaptiveCard/action",
        "adaptivecard/action",
    }:
        data = _extract_action_data(activity)
        card = _card_for_action(data, activity)
        # Card refresh response for Teams Adaptive Card Action.Execute
        return Response(
            {
                "statusCode": 200,
                "type": "application/vnd.microsoft.card.adaptive",
                "value": card,
            }
        )

    # First install / welcome when bot is added to team
    if activity_type in {"conversationupdate", "installationupdate"}:
        return Response({"ok": True})

    if activity_type == "message":
        text = ((activity.get("text") or "").strip().lower())
        data = {"action": "nav", "tab": "home"}
        if "admin" in text or "home" in text or "aidl" in text or text in {"hi", "hello", "help"}:
            card = _card_for_action(data, activity)
            # Reply with Adaptive Card attachment
            return Response(
                {
                    "type": "message",
                    "attachments": [
                        {
                            "contentType": "application/vnd.microsoft.card.adaptive",
                            "content": card,
                        }
                    ],
                }
            )
        return Response({"type": "message", "text": "Type **home** to open AIDL Admin Center."})

    return Response({"ok": True})
