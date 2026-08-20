"""Microsoft / Teams OAuth helpers (MSAL)."""

import secrets
from datetime import timedelta

import msal
import requests
from django.conf import settings
from django.utils import timezone

from .models import OAuthState


GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"


def microsoft_configured() -> bool:
    return bool(settings.MS_CLIENT_ID and settings.MS_CLIENT_SECRET)


def _authority() -> str:
    tenant = settings.MS_TENANT_ID or "common"
    return f"https://login.microsoftonline.com/{tenant}"


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=settings.MS_CLIENT_ID,
        client_credential=settings.MS_CLIENT_SECRET,
        authority=_authority(),
    )


def create_oauth_state(enroll_as: str) -> str:
    state = secrets.token_urlsafe(32)
    OAuthState.objects.create(
        state=state,
        enroll_as=enroll_as,
        expires_at=timezone.now() + timedelta(minutes=60),
    )
    return state


def consume_oauth_state(state: str):
    """
    Returns (enroll_as, error_code).
    error_code is None on success.
    """
    if not state:
        return None, "missing_state"
    try:
        row = OAuthState.objects.get(state=state)
    except OAuthState.DoesNotExist:
        return None, "state_not_found"
    if row.expires_at < timezone.now():
        row.delete()
        return None, "state_expired"
    enroll_as = row.enroll_as
    row.delete()
    return enroll_as, None


def build_auth_url(enroll_as: str) -> dict:
    state = create_oauth_state(enroll_as)
    app = _msal_app()
    auth_url = app.get_authorization_request_url(
        scopes=settings.MS_SCOPES,
        state=state,
        redirect_uri=settings.MS_REDIRECT_URI,
        prompt="select_account",
    )
    return {"auth_url": auth_url, "state": state, "enroll_as": enroll_as}


def exchange_code_for_token(code: str) -> dict:
    app = _msal_app()
    result = app.acquire_token_by_authorization_code(
        code=code,
        scopes=settings.MS_SCOPES,
        redirect_uri=settings.MS_REDIRECT_URI,
    )
    return result


def fetch_microsoft_profile(access_token: str) -> dict:
    response = requests.get(
        GRAPH_ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return {
        "microsoft_id": data.get("id") or "",
        "email": data.get("mail") or data.get("userPrincipalName") or "",
        "full_name": data.get("displayName") or "",
    }


def build_teams_launch_url(email: str = "") -> str:
    """
    Open Microsoft Teams web for the signed-in Microsoft account.
    Same pattern as VAPTfix: after OAuth, open https://teams.microsoft.com/
    login_hint helps select the same mailbox used during AIDL login.
    """
    from urllib.parse import quote

    base = "https://teams.microsoft.com/"
    email = (email or "").strip()
    if email:
        return f"{base}?login_hint={quote(email)}"
    return base
