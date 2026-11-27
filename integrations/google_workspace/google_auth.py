"""Sign in with Google for AIDL's Google Workspace integration.

Uses the same Google OAuth client as Google Classroom (GOOGLE_CLIENT_ID /
GOOGLE_CLIENT_SECRET) and only *calls* integrations/google_classroom/google_oauth.py
(checking Google's signed id_token), so that folder never changes for us.

Env (.env):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   shared with Google Classroom
  GOOGLE_WORKSPACE_REDIRECT_URI            optional; default <BACKEND_URL>/api/google-workspace/callback/
                                           Add it to the OAuth client's "Authorised redirect URIs"
                                           in Google Cloud → APIs & Services → Credentials.

Step 1 asks only for openid / email / profile: who the person is and their
Workspace domain (Google's verified "hd" claim).
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

import requests
from django.conf import settings

from integrations.google_classroom.google_oauth import (  # noqa: F401  (re-exported for this folder)
    AUTHORIZE_URL,
    SCOPES,
    TOKEN_URL,
    GoogleError,
    GoogleProfile,
    client_id,
    client_secret,
    configured,
    verify_id_token,
)

logger = logging.getLogger(__name__)


def redirect_uri() -> str:
    return (os.getenv("GOOGLE_WORKSPACE_REDIRECT_URI", "").strip()
            or f"{settings.BACKEND_URL.rstrip('/')}/api/google-workspace/callback/")


def auth_url(state: str, login_hint: str = "") -> str:
    query = {
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "scope": SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    if login_hint:
        query["login_hint"] = login_hint
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def profile_from_code(code: str) -> GoogleProfile:
    """Google's sign-in code → who signed in (checked against Google's keys)."""
    try:
        data = requests.post(TOKEN_URL, data={
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        }, timeout=20).json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("google workspace token exchange failed: %s", exc)
        raise GoogleError("token_exchange_failed", "Could not reach Google. Please try again.") from exc
    if not data.get("id_token"):
        logger.warning("google workspace token exchange error: %s", data.get("error"))
        raise GoogleError("token_exchange_failed",
                          data.get("error_description") or "Google sign-in failed. Please try again.")
    return verify_id_token(data["id_token"])
