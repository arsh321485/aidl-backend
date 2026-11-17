"""Google Chat API: encrypted admin token, the AIDL space, and the home card."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from datetime import timedelta

import jwt
import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

CHAT_API = "https://chat.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_APP_TOKEN: dict = {}


def _fernet() -> Fernet:
    key = hashlib.sha256(f"aidl-google-chat:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode() if token else ""


def decrypt_token(stored: str) -> str:
    if not stored:
        return ""
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except InvalidToken:
        logger.warning("google chat token could not be decrypted")
        return ""


def chat_api(method: str, path: str, token: str, *, body: dict | None = None, params: dict | None = None) -> dict:
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        response = requests.request(
            method,
            f"{CHAT_API}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            params=params,
            timeout=20,
        )
        data = response.json() if response.content else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("google chat %s %s failed: %s", method, path, exc)
        return {"ok": False, "error": "request_failed"}
    if response.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        code = err.get("status") if isinstance(err, dict) else err
        logger.warning("google chat %s %s -> %s", method, path, code or response.status_code)
        return {"ok": False, "error": code or "request_failed", "status": response.status_code}
    if not isinstance(data, dict):
        data = {}
    data["ok"] = True
    return data


def _service_account() -> dict | None:
    raw = getattr(settings, "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", "") or ""
    if raw:
        try:
            info = json.loads(raw)
        except ValueError:
            logger.warning("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is not valid json")
            return None
    else:
        path = getattr(settings, "GOOGLE_CHAT_SERVICE_ACCOUNT_FILE", "") or ""
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                info = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("google chat service account file unreadable: %s", exc)
            return None
    if not (info.get("client_email") and info.get("private_key")):
        return None
    return info


def app_token() -> str:
    """Chat app token, when a service account is configured. Empty otherwise."""
    cached = _APP_TOKEN.get("token")
    if cached and _APP_TOKEN.get("exp", 0) > time.time() + 60:
        return cached
    info = _service_account()
    if info is None:
        return ""
    now = int(time.time())
    try:
        assertion = jwt.encode(
            {
                "iss": info["client_email"],
                "scope": "https://www.googleapis.com/auth/chat.bot",
                "aud": TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            },
            info["private_key"],
            algorithm="RS256",
        )
        response = requests.post(
            TOKEN_URL,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            timeout=20,
        )
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("google chat app token failed: %s", exc)
        return ""
    token = data.get("access_token") or ""
    if token:
        _APP_TOKEN["token"] = token
        _APP_TOKEN["exp"] = now + int(data.get("expires_in") or 3600)
    else:
        logger.warning("google chat app token error: %s", data.get("error"))
    return token


def user_token(install) -> str:
    """The installing admin's Chat token, refreshed when it is due."""
    current = decrypt_token(install.access_token)
    if install.token_expires_at and install.token_expires_at > timezone.now() + timedelta(seconds=30) and current:
        return current
    refresh = decrypt_token(install.refresh_token)
    if not refresh:
        return current
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CHAT_CLIENT_ID,
                "client_secret": settings.GOOGLE_CHAT_CLIENT_SECRET,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("google chat token refresh failed: %s", exc)
        return current
    if not data.get("access_token"):
        logger.warning("google chat token refresh error: %s", data.get("error"))
        return current
    install.access_token = encrypt_token(data["access_token"])
    if data.get("refresh_token"):
        install.refresh_token = encrypt_token(data["refresh_token"])
    install.token_expires_at = timezone.now() + timedelta(seconds=max(int(data.get("expires_in") or 3600) - 60, 30))
    install.save(update_fields=["access_token", "refresh_token", "token_expires_at", "updated_at"])
    return data["access_token"]


def space_url(space: dict) -> str:
    if space.get("spaceUri"):
        return space["spaceUri"]
    name = space.get("name") or ""
    space_id = name.split("/")[-1] if name.startswith("spaces/") else ""
    return f"https://chat.google.com/room/{space_id}" if space_id else ""


def ensure_space(install, token: str) -> str:
    """Create the AIDL space, or reuse the one stored for this workspace."""
    if install.space_name:
        found = chat_api("GET", install.space_name, token)
        if found.get("ok") and found.get("name") and found.get("spaceType", "SPACE") != "GROUP_CHAT":
            install.space_url = space_url(found) or install.space_url
            install.save(update_fields=["space_url", "updated_at"])
            _invite_bot(install.space_name, token)
            return install.space_name

    created = chat_api(
        "POST",
        "spaces:setup",
        token,
        body={
            "space": {
                "spaceType": "SPACE",
                "displayName": settings.GOOGLE_CHAT_SPACE_NAME,
                "externalUserAllowed": False,
                "spaceDetails": {"description": "AIDL Admin Center"},
            },
        },
    )
    space = created.get("space") if isinstance(created.get("space"), dict) else created
    name = space.get("name") or ""
    if not created.get("ok") or not name.startswith("spaces/"):
        return ""
    install.space_name = name
    install.space_url = space_url(space)
    install.home_message_name = ""
    install.save(update_fields=["space_name", "space_url", "home_message_name", "updated_at"])
    _invite_bot(name, token)
    return name


def _invite_bot(space_name: str, token: str) -> None:
    bot = (getattr(settings, "GOOGLE_CHAT_BOT_USER", "") or "").strip()
    if not bot:
        return
    chat_api(
        "POST",
        f"{space_name}/members",
        token,
        body={"member": {"name": bot, "type": "BOT"}},
    )


def post_admin_card(install, user_access_token: str, card: dict, text: str) -> bool:
    """Post the Admin Center card, or refresh the one already in the space."""
    if not install.space_name:
        return False
    body = {"text": text, "cardsV2": [{"cardId": "admin-home", "card": card}]}
    token = app_token() or user_access_token
    if install.home_message_name:
        updated = chat_api(
            "PATCH",
            install.home_message_name,
            token,
            body=body,
            params={"updateMask": "text,cardsV2"},
        )
        if updated.get("ok"):
            return True
        if token != user_access_token:
            updated = chat_api(
                "PATCH",
                install.home_message_name,
                user_access_token,
                body=body,
                params={"updateMask": "text,cardsV2"},
            )
            if updated.get("ok"):
                return True
        install.home_message_name = ""
    posted = chat_api("POST", f"{install.space_name}/messages", token, body=body)
    if not posted.get("ok") and token != user_access_token:
        posted = chat_api("POST", f"{install.space_name}/messages", user_access_token, body=body)
    if not posted.get("ok"):
        return False
    install.home_message_name = posted.get("name") or ""
    install.save(update_fields=["home_message_name", "updated_at"])
    return True
