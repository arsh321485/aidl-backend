"""Bot Framework Connector client — lets the AIDL bot proactively post
Adaptive Cards into a Teams channel as itself, instead of via a delegated
user's Graph token. This is what makes a card's Action.Execute / Action.Submit
buttons actually work: Teams only routes an invoke back to a bot when the
message was sent through a real bot conversation, which Graph's chatMessage
API never creates (see api/teams_adaptive_admin.py's `interactive` cards)."""

from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_BOT_TOKEN_URL = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
_BOT_TOKEN_SCOPE = "https://api.botframework.com/.default"

_token_cache: dict = {"access_token": "", "expires_at": 0.0}


def _get_bot_token() -> str:
    """Client-credentials token for the bot's own app identity (not a user's
    delegated token) — required to call the Bot Framework Connector API."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    app_id = (getattr(settings, "MS_BOT_APP_ID", "") or "").strip()
    app_password = (getattr(settings, "MS_BOT_APP_PASSWORD", "") or "").strip()
    if not app_id or not app_password:
        logger.warning("bot framework token skipped: MS_BOT_APP_ID/MS_BOT_APP_PASSWORD not configured")
        return ""

    try:
        response = requests.post(
            _BOT_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_password,
                "scope": _BOT_TOKEN_SCOPE,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot framework token request failed: %s", exc)
        return ""

    token = data.get("access_token") or ""
    expires_in = data.get("expires_in") or 0
    if token:
        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + float(expires_in or 3600)
    return token


def _bot_headers() -> dict | None:
    token = _get_bot_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_channel_conversation(service_url: str, *, tenant_id: str, channel_id: str) -> str:
    """Start a bot conversation scoped to one Teams channel — the Bot
    Framework equivalent of "post into this channel", needed before an
    activity can be sent there. Returns "" on any failure."""
    headers = _bot_headers()
    if not headers or not service_url or not channel_id:
        return ""

    payload: dict = {
        "bot": {"id": (getattr(settings, "MS_BOT_APP_ID", "") or "").strip()},
        "isGroup": True,
        "channelData": {"channel": {"id": channel_id}},
    }
    if tenant_id:
        payload["tenantId"] = tenant_id

    url = service_url.rstrip("/") + "/v3/conversations"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code >= 400:
            logger.warning(
                "create channel conversation HTTP %s: %s",
                response.status_code,
                (response.text or "")[:500],
            )
            return ""
        return response.json().get("id") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("create channel conversation failed: %s", exc)
        return ""


def send_bot_activity(service_url: str, conversation_id: str, activity: dict) -> dict:
    """Post an activity (e.g. a message with an Adaptive Card attachment)
    into an existing bot conversation. Returns {"ok": True, ...} or
    {"ok": False, "error": "...", "detail": "..."} — same shape
    teams_messaging.send_channel_adaptive_card already returns, so callers
    can treat the two interchangeably."""
    headers = _bot_headers()
    if not headers:
        return {"ok": False, "error": "missing_bot_token"}
    if not service_url or not conversation_id:
        return {"ok": False, "error": "missing_conversation"}

    url = service_url.rstrip("/") + f"/v3/conversations/{conversation_id}/activities"
    try:
        response = requests.post(url, headers=headers, json=activity, timeout=30)
        if response.status_code >= 400:
            detail = (response.text or "")[:800]
            logger.warning("send bot activity HTTP %s: %s", response.status_code, detail)
            return {"ok": False, "error": f"bot_http_{response.status_code}", "detail": detail}
        data = response.json() if response.content else {}
        return {"ok": True, "message": data, "message_id": data.get("id")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("send bot activity failed: %s", exc)
        return {"ok": False, "error": "exception", "detail": str(exc)}


def send_channel_adaptive_card_via_bot(
    *,
    service_url: str,
    tenant_id: str,
    channel_id: str,
    card: dict,
) -> dict:
    """High-level helper: create a channel conversation, then post `card` as
    a message activity into it — the bot-backed alternative to
    teams_messaging.send_channel_adaptive_card's Graph-based post."""
    conversation_id = create_channel_conversation(service_url, tenant_id=tenant_id, channel_id=channel_id)
    if not conversation_id:
        return {"ok": False, "error": "conversation_create_failed"}

    activity = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }
    result = send_bot_activity(service_url, conversation_id, activity)
    result["conversation_id"] = conversation_id
    return result
