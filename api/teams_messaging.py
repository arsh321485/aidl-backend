"""Post Adaptive Cards to Microsoft Teams via Graph (channel Posts)."""

import json
import logging

import requests
from django.conf import settings

from .graph_client import GRAPH_BASE, graph_headers
from .teams_adaptive_admin import build_admin_adaptive_card
from .teams_cards import build_home_card


logger = logging.getLogger(__name__)


def _adaptive_card_attachment(card: dict, attachment_id: str = "aidl-adaptive-card") -> dict:
    return {
        "id": attachment_id,
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": card if isinstance(card, dict) else json.loads(card),
    }


def send_channel_adaptive_card(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    card: dict,
) -> dict | None:
    """Post an Adaptive Card into channel Posts (stays inside Teams — not a browser tab)."""
    if not access_token or not team_id or not channel_id:
        return None

    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
    attachment_id = "aidl-adaptive-card"
    payload = {
        "body": {
            "contentType": "html",
            "content": f'<attachment id="{attachment_id}"></attachment>',
        },
        "attachments": [
            {
                "id": attachment_id,
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": json.dumps(card),
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
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.text[:500] if exc.response is not None else ""
        except Exception:  # noqa: BLE001
            detail = str(exc)
        logger.warning("send channel adaptive card failed: %s %s", exc, detail)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("send channel adaptive card failed: %s", exc)
        return None


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
) -> dict | None:
    """Post dynamic Admin Center Adaptive Card into aidl dashboard Posts."""
    if not getattr(settings, "MS_SEND_WELCOME_CARD", True):
        return None
    card = build_admin_adaptive_card(
        tab,
        full_name=full_name,
        org_name=org_name,
        email=email,
        user=user,
    )
    return send_channel_adaptive_card(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=card,
    )


def send_welcome_card_after_signup(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    full_name: str = "",
    org_name: str = "",
    email: str = "",
    user=None,
) -> dict | None:
    """
    Prefer Admin Center Adaptive Card in Posts (VaptFix-style).
    Falls back to learner welcome card only if admin card build fails.
    """
    try:
        return send_admin_center_card(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            full_name=full_name,
            org_name=org_name,
            email=email,
            user=user,
            tab="home",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin center card failed, fallback welcome: %s", exc)
        card = build_home_card(full_name=full_name, org_name=org_name)
        return send_channel_adaptive_card(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            card=card,
        )
