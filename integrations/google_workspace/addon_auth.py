"""Who is calling the Gmail side panel (Google Workspace add-on, HTTP runtime).

Every request from Google carries, in authorizationEventObject:
  systemIdToken  a Google-signed ID token of our add-on deployment's service
                 account, with our endpoint's URL as audience: proves the
                 request comes from Google for OUR add-on.
  userIdToken    a Google-signed ID token of the person using Gmail (needs the
                 userinfo.email scope in the deployment): their Google id (sub),
                 email and Workspace domain (hd).

Settings (environment):
  GOOGLE_ADDON_SERVICE_ACCOUNT  the deployment's "Service Account Email"
                                (Google Cloud > Workspace Marketplace SDK > HTTP deployments)
  GOOGLE_ADDON_CLIENT_ID        optional: the expected audience of userIdToken
Without GOOGLE_ADDON_SERVICE_ACCOUNT the side panel is switched off (every request refused).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from django.conf import settings

GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")

_jwks: jwt.PyJWKClient | None = None


class AddonAuthError(Exception):
    pass


@dataclass
class AddonUser:
    sub: str
    email: str
    email_verified: bool
    hd: str
    name: str


def service_account() -> str:
    return os.getenv("GOOGLE_ADDON_SERVICE_ACCOUNT", "").strip()


def enabled() -> bool:
    return bool(service_account())


def endpoint(path: str) -> str:
    """The full URL Google calls (and the systemIdToken's audience)."""
    return f"{settings.BACKEND_URL.rstrip('/')}{path}"


def _decode(token: str, audience: str | list[str] | None) -> dict:
    global _jwks
    if not token:
        raise AddonAuthError("missing token")
    if _jwks is None:
        _jwks = jwt.PyJWKClient(GOOGLE_CERTS, cache_keys=True, lifespan=3600)
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        options = {"verify_aud": audience is not None, "require": ["exp", "iat", "iss", "sub"]}
        data = jwt.decode(token, key, algorithms=["RS256"], audience=audience, options=options, leeway=30)
    except jwt.PyJWTError as exc:
        raise AddonAuthError(f"bad token: {exc}") from exc
    if data.get("iss") not in ISSUERS:
        raise AddonAuthError("bad issuer")
    return data


def verify(event: dict, path: str) -> AddonUser:
    """The person behind a side-panel request, or AddonAuthError."""
    account = service_account()
    if not account:
        raise AddonAuthError("the Gmail add-on isn't configured")
    auth = (event or {}).get("authorizationEventObject") or {}
    url = endpoint(path)
    system = _decode(auth.get("systemIdToken", ""), [url, url.rstrip("/")])
    if system.get("email", "").lower() != account.lower() or not system.get("email_verified", False):
        raise AddonAuthError("not our add-on's service account")
    client_id = os.getenv("GOOGLE_ADDON_CLIENT_ID", "").strip() or None
    person = _decode(auth.get("userIdToken", ""), client_id)
    if not person.get("email"):
        raise AddonAuthError("no user email (add the userinfo.email scope to the deployment)")
    return AddonUser(sub=str(person["sub"]), email=str(person["email"]).lower(),
                     email_verified=bool(person.get("email_verified")), hd=str(person.get("hd") or "").lower(),
                     name=str(person.get("name") or ""))
