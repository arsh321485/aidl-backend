"""Sign in with Slack (OpenID Connect) helpers.

https://api.slack.com/authentication/sign-in-with-slack
"""

import logging
from urllib.parse import urlencode

import requests
from django.conf import settings

from .microsoft_auth import create_oauth_state

logger = logging.getLogger(__name__)

SLACK_AUTHORIZE_URL = "https://slack.com/openid/connect/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/openid.connect.token"
SLACK_USERINFO_URL = "https://slack.com/api/openid.connect.userInfo"


def slack_configured() -> bool:
    return bool(settings.SLACK_CLIENT_ID and settings.SLACK_CLIENT_SECRET)


def build_slack_auth_url(enroll_as: str) -> dict:
    state = create_oauth_state(enroll_as)
    scopes = " ".join(s.strip() for s in settings.SLACK_SCOPES.split(",") if s.strip())
    query = {
        "response_type": "code",
        "scope": scopes,
        "client_id": settings.SLACK_CLIENT_ID,
        "redirect_uri": settings.SLACK_REDIRECT_URI,
        "state": state,
    }
    return {
        "auth_url": f"{SLACK_AUTHORIZE_URL}?{urlencode(query)}",
        "state": state,
        "enroll_as": enroll_as,
    }


def exchange_slack_code(code: str) -> dict:
    """Returns Slack's token response ({"ok": true, "access_token": ...} or {"ok": false, "error": ...})."""
    try:
        response = requests.post(
            SLACK_TOKEN_URL,
            data={
                "client_id": settings.SLACK_CLIENT_ID,
                "client_secret": settings.SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.SLACK_REDIRECT_URI,
            },
            timeout=20,
        )
        return response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack token exchange failed: %s", exc)
        return {"ok": False, "error": "token_request_failed"}


def fetch_slack_profile(access_token: str) -> dict:
    """Maps Slack's OIDC userInfo to the profile shape used for AIDLUser."""
    try:
        response = requests.get(
            SLACK_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack userInfo failed: %s", exc)
        return {}
    if not data.get("ok"):
        logger.warning("slack userInfo error: %s", data.get("error"))
        return {}

    team_id = data.get("https://slack.com/team_id") or ""
    user_id = data.get("https://slack.com/user_id") or data.get("sub") or ""
    return {
        "slack_id": f"slack:{team_id}:{user_id}" if user_id else "",
        "team_id": team_id,
        "email": data.get("email") or "",
        "email_verified": bool(data.get("email_verified")),
        "first_name": data.get("given_name") or "",
        "last_name": data.get("family_name") or "",
        "full_name": data.get("name") or "",
        "avatar_url": data.get("picture") or "",
        "team_name": data.get("https://slack.com/team_name") or "",
    }
