"""Microsoft / Teams OAuth helpers (MSAL) + Graph channel deep links."""

import logging
import secrets
from datetime import timedelta
from urllib.parse import quote

import msal
import requests
from django.conf import settings
from django.utils import timezone

from .models import OAuthState


logger = logging.getLogger(__name__)

GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


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
    Generic Teams web URL with login_hint (fallback when no AIDL channel deep link).
    """
    base = "https://teams.microsoft.com/"
    email = (email or "").strip()
    if email:
        return f"{base}?login_hint={quote(email)}"
    return base


def build_channel_deep_link(
    *,
    team_id: str,
    channel_id: str,
    channel_name: str,
    tenant_id: str = "",
    email: str = "",
) -> str:
    """
    Deep link that opens a specific Teams channel.
    https://teams.microsoft.com/l/channel/{channelId}/{name}?groupId={teamId}&tenantId=...
    """
    enc_channel = quote(channel_id, safe="")
    enc_name = quote(channel_name or "AIDL", safe="")
    tenant = (tenant_id or settings.MS_TENANT_ID or "").strip()
    url = (
        f"https://teams.microsoft.com/l/channel/{enc_channel}/{enc_name}"
        f"?groupId={quote(team_id, safe='')}"
    )
    if tenant and tenant.lower() != "common":
        url += f"&tenantId={quote(tenant, safe='')}"
    email = (email or "").strip()
    if email:
        url += f"&login_hint={quote(email)}"
    return url


def _graph_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def list_team_channels(access_token: str, team_id: str) -> list:
    url = f"{GRAPH_BASE}/teams/{team_id}/channels"
    response = requests.get(url, headers=_graph_headers(access_token), timeout=20)
    response.raise_for_status()
    return response.json().get("value") or []


def create_team_channel(
    access_token: str,
    team_id: str,
    display_name: str,
    description: str = "AIDL learning and licence channel",
) -> dict:
    url = f"{GRAPH_BASE}/teams/{team_id}/channels"
    payload = {
        "displayName": display_name,
        "description": description,
        "membershipType": "standard",
    }
    response = requests.post(
        url,
        headers=_graph_headers(access_token),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def ensure_aidl_channel(access_token: str, email: str = "") -> dict | None:
    """
    Find or create the AIDL channel in MS_AIDL_TEAM_ID.
    Returns {team_id, channel_id, channel_name, teams_url} or None if not configured / Graph fails.
    Login must not break if this fails — caller should fall back to build_teams_launch_url.
    """
    team_id = (getattr(settings, "MS_AIDL_TEAM_ID", None) or "").strip()
    if not team_id:
        return None

    channel_name = (getattr(settings, "MS_AIDL_CHANNEL_NAME", None) or "AIDL").strip() or "AIDL"
    if not access_token:
        return None

    try:
        channels = list_team_channels(access_token, team_id)
        match = next(
            (
                c
                for c in channels
                if (c.get("displayName") or "").strip().lower() == channel_name.lower()
            ),
            None,
        )
        if match is None:
            created = create_team_channel(access_token, team_id, channel_name)
            channel_id = created.get("id") or ""
            channel_name = created.get("displayName") or channel_name
        else:
            channel_id = match.get("id") or ""
            channel_name = match.get("displayName") or channel_name

        if not channel_id:
            logger.warning("AIDL channel ensure: missing channel id for team %s", team_id)
            return None

        teams_url = build_channel_deep_link(
            team_id=team_id,
            channel_id=channel_id,
            channel_name=channel_name,
            tenant_id=settings.MS_TENANT_ID or "",
            email=email,
        )
        return {
            "team_id": team_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "teams_url": teams_url,
        }
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.text[:500] if exc.response is not None else ""
        except Exception:  # noqa: BLE001
            detail = str(exc)
        logger.warning("AIDL channel ensure failed: %s %s", exc, detail)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("AIDL channel ensure failed: %s", exc)
        return None


def resolve_teams_url(
    *,
    email: str = "",
    ms_access_token: str = "",
    team_id: str = "",
    channel_id: str = "",
    channel_name: str = "",
) -> str:
    """
    Prefer channel deep link (from Graph ensure or stored ids), else login_hint fallback.
    """
    name = (channel_name or getattr(settings, "MS_AIDL_CHANNEL_NAME", None) or "AIDL").strip()
    tid = (team_id or getattr(settings, "MS_AIDL_TEAM_ID", None) or "").strip()
    cid = (channel_id or "").strip()

    if tid and cid:
        return build_channel_deep_link(
            team_id=tid,
            channel_id=cid,
            channel_name=name,
            tenant_id=settings.MS_TENANT_ID or "",
            email=email,
        )

    if ms_access_token and tid:
        ensured = ensure_aidl_channel(ms_access_token, email=email)
        if ensured and ensured.get("teams_url"):
            return ensured["teams_url"]

    return build_teams_launch_url(email)
