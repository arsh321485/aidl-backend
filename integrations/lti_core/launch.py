"""The LTI 1.3 login, in two steps (the same for every LMS).

  Step 1  login_redirect()   The LMS calls our "login URL". We save a state +
                             nonce and send the browser back to the LMS's
                             auth URL.
  Step 2  verify_launch()    The LMS posts a signed id_token (a JWT) to our
                             "launch URL". We check the state, the signature
                             (with the LMS's public keys), who it is for and
                             the nonce, and return the person's details.

Nothing here knows about AIDL users or organizations; each LMS folder
(integrations/moodle/, ...) decides what to do with the result.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlencode

import jwt
from django.utils import timezone

from .models import LtiLoginState, LtiPlatform

logger = logging.getLogger(__name__)

STATE_MINUTES = 10
CLAIM = "https://purl.imsglobal.org/spec/lti/claim/"
RESOURCE_LINK_REQUEST = "LtiResourceLinkRequest"

# LTI role URIs that make someone a teacher / admin in their LMS.
STAFF_ROLES = (
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor",
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Administrator",
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Administrator",
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Instructor",
    "http://purl.imsglobal.org/vocab/lis/v2/system/person#Administrator",
    "http://purl.imsglobal.org/vocab/lis/v2/system/person#SysAdmin",
)


class LtiError(Exception):
    """The login can't go on. The message is safe to show to the person."""


@dataclass
class LaunchData:
    """Who opened AIDL, from where. `claims` keeps everything the LMS sent."""

    platform: LtiPlatform
    user_id: str  # the LMS's id for this person ("sub")
    email: str
    full_name: str
    first_name: str
    last_name: str
    roles: list[str]
    context_id: str  # the course
    context_title: str
    platform_name: str  # the LMS site's own name, e.g. "Green Valley School"
    claims: dict = field(repr=False, default_factory=dict)

    @property
    def is_staff(self) -> bool:
        return any(r in STAFF_ROLES for r in self.roles)


# ---------- step 1 ----------

def find_platform(issuer: str, client_id: str, kind: str) -> LtiPlatform:
    rows = LtiPlatform.objects.filter(issuer=issuer.rstrip("/"), kind=kind, is_active=True)
    if client_id:
        rows = rows.filter(client_id=client_id)
    platforms = list(rows[:2])
    if len(platforms) != 1:
        raise LtiError("This site isn't registered with AIDL yet. Ask your site admin to finish the AIDL setup.")
    return platforms[0]


def login_redirect(params, kind: str, redirect_uri: str) -> str:
    """The LMS's login request (GET or POST params) → the URL to send the browser to."""
    issuer = (params.get("iss") or "").strip()
    login_hint = params.get("login_hint") or ""
    if not issuer or not login_hint:
        raise LtiError("The login request is missing iss or login_hint. Open AIDL from your course again.")
    platform = find_platform(issuer, (params.get("client_id") or "").strip(), kind)

    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    LtiLoginState.objects.create(state=state, nonce=nonce, platform_id=str(platform.pk),
                                 expires_at=timezone.now() + timedelta(minutes=STATE_MINUTES))
    query = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": platform.client_id,
        "redirect_uri": redirect_uri,
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
    }
    if params.get("lti_message_hint"):
        query["lti_message_hint"] = params["lti_message_hint"]
    return f"{platform.auth_login_url}?{urlencode(query)}"


# ---------- step 2 ----------

_JWK_CLIENTS: dict[str, jwt.PyJWKClient] = {}


def _signing_key(platform: LtiPlatform, token: str):
    client = _JWK_CLIENTS.get(platform.jwks_url)
    if client is None:
        client = _JWK_CLIENTS[platform.jwks_url] = jwt.PyJWKClient(platform.jwks_url, cache_keys=True, timeout=15)
    try:
        return client.get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientError as exc:
        logger.warning("LTI keys from %s: %s", platform.jwks_url, exc)
        raise LtiError("AIDL couldn't read your site's signing keys. Please try again in a minute.") from exc


def _use_state(state: str) -> LtiLoginState:
    row = LtiLoginState.objects.filter(state=state).first() if state else None
    if row is None or row.used_at is not None or row.expires_at < timezone.now():
        raise LtiError("This login link has expired. Open AIDL from your course again.")
    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])
    return row


def verify_launch(post) -> LaunchData:
    """The LMS's form post (id_token + state) → LaunchData, or LtiError."""
    if post.get("error"):
        raise LtiError(f"Your site refused the login: {post.get('error_description') or post.get('error')}")
    token = post.get("id_token") or ""
    if not token:
        raise LtiError("No login token arrived. Open AIDL from your course again.")
    saved = _use_state(post.get("state") or "")
    platform = LtiPlatform.objects.filter(pk=saved.platform_id, is_active=True).first()
    if platform is None:
        raise LtiError("This site isn't registered with AIDL any more.")

    try:
        claims = jwt.decode(token, _signing_key(platform, token), algorithms=["RS256"], audience=platform.client_id,
                            issuer=platform.issuer, options={"require": ["exp", "iat", "sub", "nonce"]}, leeway=60)
    except jwt.PyJWTError as exc:
        logger.warning("LTI token rejected for %s: %s", platform, exc)
        raise LtiError("The login token didn't check out. Open AIDL from your course again.") from exc

    # With several audiences, "azp" must name us (OpenID Connect rule).
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and claims.get("azp") != platform.client_id:
        raise LtiError("The login token wasn't meant for AIDL.")
    if claims.get("nonce") != saved.nonce:
        raise LtiError("This login was already used. Open AIDL from your course again.")
    if claims.get(CLAIM + "version") != "1.3.0":
        raise LtiError("AIDL needs LTI version 1.3.")
    if claims.get(CLAIM + "message_type") != RESOURCE_LINK_REQUEST:
        raise LtiError("AIDL can only be opened from a course link for now.")
    if not platform.has_deployment(claims.get(CLAIM + "deployment_id", "")):
        raise LtiError("This AIDL install isn't registered yet. Ask your site admin to add its deployment id.")

    context = claims.get(CLAIM + "context") or {}
    tool_platform = claims.get(CLAIM + "tool_platform") or {}
    first, last = claims.get("given_name", ""), claims.get("family_name", "")
    return LaunchData(
        platform=platform,
        user_id=str(claims["sub"]),
        email=(claims.get("email") or "").strip(),
        full_name=(claims.get("name") or f"{first} {last}").strip(),
        first_name=first,
        last_name=last,
        roles=list(claims.get(CLAIM + "roles") or []),
        context_id=str(context.get("id", "")),
        context_title=context.get("title") or context.get("label") or "",
        platform_name=tool_platform.get("name") or "",
        claims=claims,
    )
