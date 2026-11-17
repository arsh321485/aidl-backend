"""Sign in with Google (OpenID Connect) for Google Classroom.

https://developers.google.com/identity/openid-connect/openid-connect

Env (.env):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   Google Cloud → APIs & Services → Credentials
                                           → OAuth client ID (type: Web application)
  GOOGLE_CLASSROOM_REDIRECT_URI            optional; default <BACKEND_URL>/api/google-classroom/callback/

Sign-in asks only for openid/email/profile. The Classroom scopes (courses,
rosters) will be asked for later, when we read classes, with
include_granted_scopes so the person just adds them to what they granted.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPES = "openid email profile"
# Step 2: read a teacher's classes and their students (asked for separately,
# only by admins, on the classes page).
CLASSROOM_SCOPES = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.profile.emails",
)

_jwks_client: jwt.PyJWKClient | None = None


class GoogleError(Exception):
    """Sign-in failed; the message is safe to show to the person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class GoogleProfile:
    sub: str  # Google's permanent user id
    email: str
    email_verified: bool
    hd: str  # Workspace domain ("" for a personal Gmail account)
    first_name: str
    last_name: str
    full_name: str
    picture: str


def client_id() -> str:
    return os.getenv("GOOGLE_CLIENT_ID", "").strip()


def client_secret() -> str:
    return os.getenv("GOOGLE_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    return (os.getenv("GOOGLE_CLASSROOM_REDIRECT_URI", "").strip()
            or f"{settings.BACKEND_URL}/api/google-classroom/callback/")


def configured() -> bool:
    return bool(client_id() and client_secret())


def auth_url(state: str, classroom: bool = False, login_hint: str = "") -> str:
    """classroom=True also asks for the Classroom scopes, offline (a refresh
    token, so classes can be fetched again later without the person)."""
    query = {
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "scope": " ".join((SCOPES, *CLASSROOM_SCOPES)) if classroom else SCOPES,
        "state": state,
        "access_type": "offline" if classroom else "online",
        "include_granted_scopes": "true",
        # consent: Google only hands out a refresh token on the consent screen.
        "prompt": "consent" if classroom else "select_account",
    }
    if login_hint:
        query["login_hint"] = login_hint
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def exchange_code(code: str) -> str:
    """Authorization code → Google's id_token (a signed JWT about the person)."""
    return exchange_tokens(code)["id_token"]


def exchange_tokens(code: str) -> dict:
    """Authorization code → Google's token response (id_token, access_token,
    refresh_token when offline, scope)."""
    try:
        resp = requests.post(TOKEN_URL, data={
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
        }, timeout=20)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("google token exchange failed: %s", exc)
        raise GoogleError("token_exchange_failed", "Could not reach Google. Please try again.") from exc
    if not data.get("id_token"):
        logger.warning("google token exchange error: %s", data.get("error"))
        raise GoogleError("token_exchange_failed",
                          data.get("error_description") or "Google sign-in failed. Please try again.")
    return data


def refresh_access_token(refresh_token: str) -> str:
    """A stored refresh token → a fresh access token (valid ~1 hour)."""
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id(),
            "client_secret": client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=20)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("google token refresh failed: %s", exc)
        raise GoogleError("refresh_failed", "Could not reach Google. Please try again.") from exc
    if not data.get("access_token"):
        logger.warning("google token refresh error: %s", data.get("error"))
        # invalid_grant = the person removed AIDL's access in their Google account.
        raise GoogleError("reconnect", "Google Classroom access has expired. Please connect again.")
    return data["access_token"]


def has_classroom_scopes(granted: str) -> bool:
    """Google lets people untick permissions on the consent screen."""
    return set(CLASSROOM_SCOPES) <= set((granted or "").split())


def _signing_key(id_token: str):
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(CERTS_URL, cache_keys=True)
    return _jwks_client.get_signing_key_from_jwt(id_token).key


def verify_id_token(id_token: str) -> GoogleProfile:
    try:
        claims = jwt.decode(id_token, _signing_key(id_token), algorithms=["RS256"], audience=client_id(),
                            options={"require": ["iss", "sub", "aud", "exp", "iat"]}, leeway=60)
    except Exception as exc:  # noqa: BLE001
        logger.warning("google id_token rejected: %s", exc)
        raise GoogleError("invalid_token", "Google sign-in could not be verified. Please try again.") from exc
    if claims.get("iss") not in ISSUERS:
        raise GoogleError("invalid_token", "Google sign-in could not be verified. Please try again.")
    first, last = claims.get("given_name") or "", claims.get("family_name") or ""
    return GoogleProfile(
        sub=str(claims["sub"]),
        email=(claims.get("email") or "").strip().lower(),
        email_verified=bool(claims.get("email_verified")),
        hd=(claims.get("hd") or "").strip().lower(),
        first_name=first,
        last_name=last,
        full_name=claims.get("name") or f"{first} {last}".strip(),
        picture=claims.get("picture") or "",
    )
