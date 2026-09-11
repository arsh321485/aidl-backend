"""Post Adaptive Cards to Microsoft Teams via Graph (channel Posts)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from django.conf import settings

from .graph_client import GRAPH_BASE, graph_headers
from .teams_adaptive_admin import build_admin_adaptive_card
from .teams_cards import build_home_card


logger = logging.getLogger(__name__)


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
            "content": f'<attachment id="{attachment_id}"></attachment>',
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


def _post_with_retries(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    card: dict,
    attempts: int = 3,
    delay_sec: float = 2.0,
) -> dict:
    """Retry Graph posts — new channels are sometimes not message-ready yet."""
    last: dict[str, Any] = {"ok": False, "error": "no_attempts"}
    for i in range(max(1, attempts)):
        if i > 0:
            time.sleep(delay_sec * i)
        last = send_channel_adaptive_card(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            card=card,
        )
        if last.get("ok"):
            last["attempt"] = i + 1
            return last
    return last


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
    channel_just_created: bool = False,
) -> dict:
    """Post dynamic Admin Center Adaptive Card into aidl dashboard Posts."""
    if not getattr(settings, "MS_SEND_WELCOME_CARD", True):
        return {"ok": False, "error": "disabled_by_settings"}

    if channel_just_created:
        # Graph often needs a beat before a brand-new channel accepts messages.
        time.sleep(2.5)

    try:
        # Phase 1: Graph-safe card (no Action.Execute) so Posts is never empty.
        card = build_admin_adaptive_card(
            tab,
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
            interactive=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("build admin adaptive card failed")
        return {"ok": False, "error": "card_build_failed", "detail": str(exc)}

    result = _post_with_retries(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=card,
        attempts=3,
        delay_sec=2.0,
    )
    if result.get("ok"):
        result["card_variant"] = "graph_safe"
        return result

    # Minimal fallback so Posts is never the empty Teams welcome screen.
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
                    "Full dashboard layout could not be posted. "
                    "Re-login after ChannelMessage.Send admin consent."
                ),
                "wrap": True,
                "isSubtle": True,
            },
        ],
        "msteams": {"width": "Full"},
    }
    last = _post_with_retries(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=minimal,
        attempts=2,
        delay_sec=2.0,
    )
    last["card_variant"] = "minimal"
    last["first_error"] = result
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
    channel_just_created: bool = False,
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
        channel_just_created=channel_just_created,
    )
    if result.get("ok"):
        return result

    # Absolute fallback: learner welcome card
    try:
        card = build_home_card(full_name=full_name, org_name=org_name)
        fallback = _post_with_retries(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            card=card,
            attempts=2,
            delay_sec=2.0,
        )
        fallback["card_variant"] = "learner_welcome"
        fallback["admin_error"] = result
        return fallback
    except Exception as exc:  # noqa: BLE001
        logger.warning("learner welcome fallback failed: %s", exc)
        result["fallback_error"] = str(exc)
        return result
