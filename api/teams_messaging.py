"""Post Adaptive Cards to Microsoft Teams via Graph (channel Posts)."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from django.conf import settings

from .graph_client import GRAPH_BASE, graph_headers
from .teams_adaptive_admin import build_admin_adaptive_card
from .teams_cards import build_home_card


logger = logging.getLogger(__name__)


def _strip_execute_actions(card: dict) -> dict:
    """
    Graph user-delegated posts may reject / poorly handle Action.Execute.
    For Phase 1 visibility, keep display + OpenUrl; bot phase restores Execute.
    """
    import copy

    cloned = copy.deepcopy(card)

    def scrub(node: Any) -> None:
        if isinstance(node, dict):
            if "actions" in node and isinstance(node["actions"], list):
                cleaned = []
                for action in node["actions"]:
                    if not isinstance(action, dict):
                        continue
                    if action.get("type") == "Action.Execute":
                        # Convert nav labels to plain text hint instead of dropping UI.
                        continue
                    cleaned.append(action)
                node["actions"] = cleaned
            if node.get("type") == "ActionSet" and isinstance(node.get("actions"), list):
                # Keep Execute actions in ActionSet for when bot is present;
                # Graph still usually accepts them as non-functional buttons.
                pass
            for value in node.values():
                scrub(value)
        elif isinstance(node, list):
            for item in node:
                scrub(item)

    scrub(cloned)
    return cloned


def send_channel_adaptive_card(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    card: dict,
) -> dict:
    """
    Post Adaptive Card into channel Posts.
    Returns {"ok": True, "message": {...}} or {"ok": False, "error": "...", "detail": "..."}.
    """
    if not access_token:
        return {"ok": False, "error": "missing_access_token"}
    if not team_id or not channel_id:
        return {"ok": False, "error": "missing_team_or_channel"}

    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
    attachment_id = "aidlAdminCard"
    payload = {
        "subject": "AIDL Admin Center",
        "body": {
            "contentType": "html",
            "content": (
                "<p><strong>AIDL Admin Center</strong></p>"
                f'<attachment id="{attachment_id}"></attachment>'
            ),
        },
        "attachments": [
            {
                "id": attachment_id,
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": json.dumps(card),
                "name": None,
                "thumbnailUrl": None,
            }
        ],
    }
    try:
        response = requests.post(
            url,
            headers=graph_headers(access_token),
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            detail = (response.text or "")[:800]
            logger.warning(
                "send channel adaptive card HTTP %s: %s",
                response.status_code,
                detail,
            )
            return {
                "ok": False,
                "error": f"graph_http_{response.status_code}",
                "detail": detail,
            }
        data = response.json() if response.content else {}
        return {"ok": True, "message": data, "message_id": data.get("id")}
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.text[:800] if exc.response is not None else ""
        except Exception:  # noqa: BLE001
            detail = str(exc)
        logger.warning("send channel adaptive card failed: %s %s", exc, detail)
        return {"ok": False, "error": "http_error", "detail": detail or str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("send channel adaptive card failed: %s", exc)
        return {"ok": False, "error": "exception", "detail": str(exc)}


def send_admin_center_card(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
    tab: str = "home",
) -> dict:
    """Post dynamic Admin Center Adaptive Card into aidl dashboard Posts."""
    if not getattr(settings, "MS_SEND_WELCOME_CARD", True):
        return {"ok": False, "error": "disabled_by_settings"}

    try:
        card = build_admin_adaptive_card(
            tab,
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("build admin adaptive card failed")
        return {"ok": False, "error": "card_build_failed", "detail": str(exc)}

    # First try full card (with Execute nav for bot).
    result = send_channel_adaptive_card(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=card,
    )
    if result.get("ok"):
        result["card_variant"] = "full"
        return result

    # Retry with Execute actions stripped from top-level actions (keep ActionSet).
    stripped = _strip_execute_actions(card)
    # Ensure at least one OpenUrl so card has an action.
    if not stripped.get("actions"):
        stripped["actions"] = [
            {
                "type": "Action.OpenUrl",
                "title": "Open AIDL Admin API",
                "url": getattr(settings, "MS_TEAMS_APP_BASE_URL", "")
                or "https://aidl-backend.onrender.com/api/teams/",
            }
        ]
    retry = send_channel_adaptive_card(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=stripped,
    )
    if retry.get("ok"):
        retry["card_variant"] = "stripped"
        retry["first_error"] = result
        return retry

    # Last resort: simple text + minimal card so Posts is never empty.
    minimal = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"Welcome to AIDL Admin Center, {full_name or 'Admin'}!",
                "weight": "Bolder",
                "size": "Large",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": f"Organisation: {org_name or 'AIDL'}",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    "Admin dashboard card could not load full layout. "
                    "Re-login after ChannelMessage.Send consent, or open bot."
                ),
                "wrap": True,
                "isSubtle": True,
            },
        ],
    }
    last = send_channel_adaptive_card(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=minimal,
    )
    last["card_variant"] = "minimal"
    last["first_error"] = result
    last["second_error"] = retry
    return last


def send_welcome_card_after_signup(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
) -> dict:
    """Always try Admin Center card in Posts; return structured result."""
    result = send_admin_center_card(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
        tab="home",
    )
    if result.get("ok"):
        return result

    # Absolute fallback: learner welcome card
    try:
        card = build_home_card(full_name=full_name, org_name=org_name)
        fallback = send_channel_adaptive_card(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            card=card,
        )
        fallback["card_variant"] = "learner_welcome"
        fallback["admin_error"] = result
        return fallback
    except Exception as exc:  # noqa: BLE001
        logger.warning("learner welcome fallback failed: %s", exc)
        result["fallback_error"] = str(exc)
        return result
