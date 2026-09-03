"""Post Adaptive Cards to Microsoft Teams via Graph."""

import json
import logging

import requests
from django.conf import settings

from .graph_client import GRAPH_BASE, graph_headers
from .teams_cards import build_home_card


logger = logging.getLogger(__name__)


def _adaptive_card_attachment(card: dict) -> dict:
    return {
        "id": "aidl-adaptive-card",
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": json.dumps(card),
    }


def send_channel_adaptive_card(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    card: dict,
) -> dict | None:
    """Post an Adaptive Card to a Teams channel. Requires ChannelMessage.Send."""
    if not access_token or not team_id or not channel_id:
        return None

    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
    payload = {
        "body": {
            "contentType": "html",
            "content": '<attachment id="aidl-adaptive-card"></attachment>',
        },
        "attachments": [_adaptive_card_attachment(card)],
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


def send_welcome_card_after_signup(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    full_name: str = "",
    org_name: str = "",
) -> dict | None:
    """Send the Home welcome Adaptive Card to the AIDL channel."""
    if not getattr(settings, "MS_SEND_WELCOME_CARD", True):
        return None
    card = build_home_card(full_name=full_name, org_name=org_name)
    return send_channel_adaptive_card(
        access_token,
        team_id=team_id,
        channel_id=channel_id,
        card=card,
    )
