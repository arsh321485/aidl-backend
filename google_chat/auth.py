"""Sign in with Google, and the extra Chat scopes an organization install needs."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import requests
from django.conf import settings

from api.microsoft_auth import create_oauth_state

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

IDENTITY_SCOPES = "openid email profile"


def configured() -> bool:
    return bool(settings.GOOGLE_CHAT_CLIENT_ID and settings.GOOGLE_CHAT_CLIENT_SECRET)


def build_login_url(enroll_as: str, payload: str = "") -> dict:
    state = create_oauth_state(enroll_as, payload)
    scopes = settings.GOOGLE_CHAT_SCOPES if enroll_as == "organization" else IDENTITY_SCOPES
    query = {
        "client_id": settings.GOOGLE_CHAT_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_CHAT_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes.split()),
        "state": state,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent" if enroll_as == "organization" else "select_account",
    }
    return {
        "auth_url": f"{AUTHORIZE_URL}?{urlencode(query)}",
        "state": state,
        "enroll_as": enroll_as,
    }


def exchange_code(code: str) -> dict:
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CHAT_CLIENT_ID,
                "client_secret": settings.GOOGLE_CHAT_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_CHAT_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("google token exchange failed: %s", exc)
        return {"ok": False, "error": "token_request_failed"}
    if not data.get("access_token"):
        logger.warning("google token exchange error: %s", data.get("error"))
        return {"ok": False, "error": data.get("error") or "token_request_failed"}
    data["ok"] = True
    return data


def fetch_profile(access_token: str) -> dict:
    try:
        response = requests.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("google userinfo failed: %s", exc)
        return {}
    sub = str(data.get("sub") or "")
    if not sub or data.get("error"):
        logger.warning("google userinfo error: %s", data.get("error"))
        return {}
    return {
        "sub": sub,
        "google_id": f"google:{sub}",
        "hosted_domain": str(data.get("hd") or "").strip().lower(),
        "email": data.get("email") or "",
        "email_verified": bool(data.get("email_verified")),
        "first_name": data.get("given_name") or "",
        "last_name": data.get("family_name") or "",
        "full_name": data.get("name") or "",
        "avatar_url": data.get("picture") or "",
    }
