"""Slack Web API for the organization install (AIDL Slack guide, sections 6
and 7): "Add to Slack" OAuth v2, the #aidl channel, and the Admin Center
message posted in it."""

from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from .microsoft_auth import create_oauth_state
from .models import Organization

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
SLACK_INSTALL_URL = "https://slack.com/oauth/v2/authorize"


# ---------- bot token at rest (guide: "Store the bot token encrypted") ----------

def _fernet() -> Fernet:
    key = hashlib.sha256(f"aidl-slack:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode() if token else ""


def bot_token(org: Organization) -> str:
    if not org.slack_bot_token:
        return ""
    try:
        return _fernet().decrypt(org.slack_bot_token.encode()).decode()
    except InvalidToken:
        logger.warning("slack bot token for org %s could not be decrypted", org.pk)
        return ""


# ---------- Web API ----------

def slack_api(method: str, token: str, *, json: dict | None = None, params: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if json is not None:
            resp = requests.post(f"{SLACK_API}/{method}", headers=headers, json=json, timeout=15)
        else:
            resp = requests.get(f"{SLACK_API}/{method}", headers=headers, params=params or {}, timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack %s failed: %s", method, exc)
        return {"ok": False, "error": "request_failed"}
    if not data.get("ok"):
        logger.warning("slack %s -> %s", method, data.get("error"))
    return data


# ---------- "Add to Slack" OAuth v2 ----------

def build_install_url(enroll_as: str, payload: str = "") -> dict:
    state = create_oauth_state(enroll_as, payload)
    query = {
        "client_id": settings.SLACK_CLIENT_ID,
        "scope": settings.SLACK_BOT_SCOPES,
        "redirect_uri": settings.SLACK_REDIRECT_URI,
        "state": state,
    }
    return {"auth_url": f"{SLACK_INSTALL_URL}?{urlencode(query)}", "state": state, "enroll_as": enroll_as}


def exchange_install_code(code: str) -> dict:
    try:
        resp = requests.post(
            f"{SLACK_API}/oauth.v2.access",
            data={
                "client_id": settings.SLACK_CLIENT_ID,
                "client_secret": settings.SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.SLACK_REDIRECT_URI,
            },
            timeout=20,
        )
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack oauth.v2.access failed: %s", exc)
        return {"ok": False, "error": "token_request_failed"}


def installer_profile(install: dict) -> dict:
    """Profile of the person who installed the app, in the same shape as
    slack_auth.fetch_slack_profile (read with the new bot token)."""
    team = install.get("team") or {}
    user_id = (install.get("authed_user") or {}).get("id") or ""
    data = slack_api("users.info", install.get("access_token", ""), params={"user": user_id})
    if not data.get("ok"):
        return {}
    p = (data.get("user") or {}).get("profile") or {}
    return {
        "slack_id": f"slack:{team.get('id', '')}:{user_id}",
        "slack_user_id": user_id,
        "team_id": team.get("id", ""),
        "team_name": team.get("name", ""),
        # Slack only lets members use an email they have confirmed.
        "email": p.get("email") or "",
        "email_verified": bool(p.get("email")),
        "first_name": p.get("first_name") or "",
        "last_name": p.get("last_name") or "",
        "full_name": p.get("real_name") or p.get("display_name") or "",
        "avatar_url": p.get("image_192") or "",
    }


def save_install(org: Organization, install: dict) -> None:
    org.slack_team_id = (install.get("team") or {}).get("id", "") or org.slack_team_id
    org.slack_bot_token = encrypt_token(install.get("access_token", ""))
    org.slack_bot_user_id = install.get("bot_user_id", "") or ""
    org.save(update_fields=["slack_team_id", "slack_bot_token", "slack_bot_user_id", "updated_at"])


# ---------- the #aidl channel (guide 7.1) ----------

def channel_url(org: Organization) -> str:
    if not (org.slack_team_id and org.slack_channel_id):
        return ""
    return f"https://app.slack.com/client/{org.slack_team_id}/{org.slack_channel_id}"


def _find_channel_by_name(token: str, name: str) -> str:
    cursor = ""
    for _ in range(20):
        data = slack_api(
            "conversations.list",
            token,
            params={"exclude_archived": "true", "types": "public_channel", "limit": 200, "cursor": cursor},
        )
        for ch in data.get("channels") or []:
            if ch.get("name") == name:
                return ch.get("id", "")
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return ""


def _channel_names() -> list[str]:
    name = settings.SLACK_CHANNEL_NAME
    return [name, f"{name}-app"] + [f"{name}-{n}" for n in range(2, 6)]


def _create_or_join_channel(token: str) -> str:
    """Create #aidl. If the name is taken by a public channel, join that one;
    if it's taken by something the bot can't use (a private channel), fall
    back to #aidl-app, #aidl-2, ..."""
    for name in _channel_names():
        created = slack_api("conversations.create", token, json={"name": name, "is_private": False})
        if created.get("ok"):
            return created["channel"]["id"]
        if created.get("error") != "name_taken":
            logger.warning("slack conversations.create #%s failed: %s", name, created.get("error"))
            return ""
        existing = _find_channel_by_name(token, name)
        if existing:
            joined = slack_api("conversations.join", token, json={"channel": existing})
            if joined.get("ok"):
                return existing
        logger.warning("slack channel #%s is taken and not joinable; trying the next name", name)
    return ""


def ensure_channel(org: Organization, admin_slack_user_id: str) -> str:
    """Create (or reuse) #aidl, make sure the bot and the admin are in it.
    Returns the channel id, or "" when Slack refused (e.g. missing scope)."""
    token = bot_token(org)
    if not token:
        return ""

    channel_id = org.slack_channel_id
    if channel_id:
        info = slack_api("conversations.info", token, params={"channel": channel_id})
        if not info.get("ok") or (info.get("channel") or {}).get("is_archived"):
            channel_id = ""  # deleted/archived → create a new one

    if not channel_id:
        channel_id = _create_or_join_channel(token)
        if not channel_id:
            return ""
        org.slack_channel_id = channel_id
        org.slack_home_message_ts = ""
        org.save(update_fields=["slack_channel_id", "slack_home_message_ts", "updated_at"])

    if admin_slack_user_id:
        invite = slack_api("conversations.invite", token, json={"channel": channel_id, "users": admin_slack_user_id})
        if not invite.get("ok") and invite.get("error") not in ("already_in_channel", "cant_invite_self"):
            logger.warning("could not add %s to #aidl: %s", admin_slack_user_id, invite.get("error"))
    return channel_id


def post_admin_center(org: Organization, blocks: list, text: str) -> bool:
    """Post the Admin Center Home card in #aidl — or refresh the one already
    there, so logging in again doesn't flood the channel."""
    token = bot_token(org)
    if not (token and org.slack_channel_id):
        return False
    if org.slack_home_message_ts:
        updated = slack_api(
            "chat.update",
            token,
            json={"channel": org.slack_channel_id, "ts": org.slack_home_message_ts, "blocks": blocks, "text": text},
        )
        if updated.get("ok"):
            return True
    posted = slack_api(
        "chat.postMessage",
        token,
        json={"channel": org.slack_channel_id, "blocks": blocks, "text": text, "unfurl_links": False},
    )
    if not posted.get("ok"):
        return False
    org.slack_home_message_ts = posted.get("ts", "")
    org.save(update_fields=["slack_home_message_ts", "updated_at"])
    return True
