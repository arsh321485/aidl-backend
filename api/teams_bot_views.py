"""
Teams Bot messaging endpoint — VaptFix-style in-Posts Adaptive Card navigation.

Azure Bot must call:
  POST https://aidl-backend.onrender.com/api/teams/bot/messages/
"""

from __future__ import annotations

import datetime as _dt
import logging

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import AIDLUser
from .teams_adaptive_admin import build_admin_adaptive_card
from .teams_cards import org_display_name


logger = logging.getLogger(__name__)

# Action.Execute verbs that write something, vs. "nav" which only re-renders
# a tab. Every write action needs a signed-in AIDLUser (resolved from the
# Teams activity's aadObjectId/email) — Bot Framework's own signature
# verification on the incoming activity is what stands in for a session here.
_WRITE_ACTIONS = {
    "promote_admin",
    "invite_user",
    "request_card",
    "send_card_now",
    "schedule_card",
    "request_new_card",
    "add_app",
}


def _bool_from_toggle(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _run_write_action(action: str, data: dict, user: AIDLUser | None) -> dict | None:
    """Performs the action; returns None on success or {"code","message"} on
    failure. Reuses the exact same service functions as the REST endpoints
    (admin_ops / cards_service / teams_invites) so bot and Website Tab stay
    behaviourally identical."""
    from .admin_ops import AdminOpsError, add_registered_app, promote_to_admin
    from .cards_service import CardsError, request_card, request_new_card, schedule_card, send_card_now
    from .org_service import get_organization_for_user
    from .teams_invites import send_user_invite

    if user is None:
        return {"code": "not_signed_in", "message": "Sign in to AIDL via Teams first."}

    try:
        if action == "promote_admin":
            promote_to_admin(
                user,
                email=data.get("promoteEmail") or "",
                permissions={
                    "approve_apps": _bool_from_toggle(data.get("permApproveApps", "true")),
                    "access_cards": _bool_from_toggle(data.get("permAccessCards", "true")),
                    "create_card": _bool_from_toggle(data.get("permCreateCard", "true")),
                },
            )
            return None

        if action == "invite_user":
            result = send_user_invite(
                caller=user,
                email=data.get("inviteEmail") or "",
                full_name=data.get("inviteName") or "",
            )
            if not result.get("ok"):
                return {"code": result.get("error") or "invite_failed", "message": result.get("message", "")}
            return None

        org = get_organization_for_user(user)
        if org is None:
            return {"code": "no_organization", "message": "Organisation not ready yet."}

        if action in ("request_card", "send_card_now"):
            if not user.perm_access_cards:
                return {"code": "permission_denied", "message": "Access Cards permission required."}
            card_id = data.get("card_id") or ""
            if action == "request_card":
                request_card(org, card_id=card_id, by_email=user.email)
            else:
                send_card_now(org, card_id=card_id, by_user=user)
            return None

        if action == "schedule_card":
            if not user.perm_access_cards:
                return {"code": "permission_denied", "message": "Access Cards permission required."}
            card_id = data.get("scheduleCardId") or ""
            d = parse_date(data.get("scheduleDate") or "")
            t = parse_time(data.get("scheduleTime") or "") or _dt.time(9, 0)
            if not card_id:
                return {"code": "card_required", "message": "Pick a card to schedule."}
            if not d:
                return {"code": "invalid_schedule", "message": "Pick a valid date."}
            scheduled_at = timezone.make_aware(_dt.datetime.combine(d, t), timezone.get_current_timezone())
            schedule_card(org, card_id=card_id, by_email=user.email, scheduled_at=scheduled_at)
            return None

        if action == "request_new_card":
            if not user.perm_create_card:
                return {"code": "permission_denied", "message": "Create Card permission required."}
            request_new_card(
                org,
                by_email=user.email,
                title=data.get("newCardTitle") or "",
                description=data.get("newCardDesc") or "",
            )
            return None

        if action == "add_app":
            add_registered_app(
                org,
                app_type=data.get("app_type") or "",
                name=data.get("appName") or "",
                category=data.get("appCategory") or "",
                data_allowed=data.get("appDataAllowed") or "",
                status=data.get("appStatus") or "",
            )
            return None
    except (AdminOpsError, CardsError) as exc:
        return {"code": exc.code, "message": exc.message}

    return {"code": "unknown_action", "message": ""}


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


def _task_module_response(request, data: dict, activity: dict) -> dict:
    """
    Response to a Teams Task Module task/fetch invoke — opens an in-app modal
    (an iframe Teams hosts itself) instead of Action.OpenUrl's browser tab.
    """
    from .org_service import build_admin_tab_payload
    from .teams_adaptive_admin import _nav_base_url

    action = (data.get("action") or "").strip().lower()
    user, full_name, email = _resolve_user(data, activity)
    org_name = (user.organization_name if user is not None else "") or ""

    if action == "view_policy":
        payload = build_admin_tab_payload(
            "policy", full_name=full_name, org_name=org_name, email=email, user=user
        )
        url = payload.get("policy_url") or ""
        if url.startswith("/"):
            url = request.build_absolute_uri(url)
        return {
            "task": {
                "type": "continue",
                "value": {"title": "Current Policy", "width": "large", "height": "large", "url": url},
            }
        }

    if action == "upload_policy":
        suffix = f"?email={email}" if email else ""
        return {
            "task": {
                "type": "continue",
                "value": {
                    "title": "Upload New Policy",
                    "width": "large",
                    "height": "large",
                    "url": f"{_nav_base_url()}/tabs/policy/{suffix}",
                },
            }
        }

    return {"task": {"type": "message", "value": "Unsupported action."}}


def _card_for_action(data: dict, activity: dict) -> dict:
    user, full_name, email = _resolve_user(data, activity)
    action = (data.get("action") or "nav").strip().lower()
    tab = (data.get("tab") or "home").strip().lower()
    if action == "export":
        # Stay on home; export is handled via OpenUrl in a follow-up message text.
        tab = "home"

    error = None
    if action in _WRITE_ACTIONS:
        error = _run_write_action(action, data, user)
        if user is not None:
            user.refresh_from_db()

    org_name = ""
    if user is not None:
        org_name = user.organization_name or ""
    card = build_admin_adaptive_card(
        tab,
        full_name=full_name,
        org_name=org_name or org_display_name(),
        email=email,
        user=user,
        interactive=True,
    )
    if error is not None:
        card["body"].insert(
            0,
            {
                "type": "Container",
                "style": "attention",
                "spacing": "None",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "⚠ " + (error.get("message") or error.get("code") or "Action failed"),
                        "wrap": True,
                        "size": "Small",
                        "weight": "Bolder",
                    }
                ],
            },
        )
    return card


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

    # Task Module open request (View Current Policy / Upload New Policy) →
    # in-app modal instead of Action.OpenUrl's browser tab.
    if activity_type == "invoke" and activity.get("name") == "task/fetch":
        data = _extract_action_data(activity)
        return Response(_task_module_response(request, data, activity))

    # Task Module closed after a successful submit — nothing further to do,
    # the underlying card re-renders next time its own nav pill is clicked.
    if activity_type == "invoke" and activity.get("name") == "task/submit":
        return Response({"task": {"type": "message", "value": "Done."}})

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
