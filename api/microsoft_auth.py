"""Microsoft / Teams OAuth helpers (MSAL) + Graph team/channel deep links."""

import logging
import secrets
import time
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
    """Real Microsoft Teams platform URL with login_hint."""
    base = "https://teams.microsoft.com/v2/"
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
    """Deep link that opens a specific Teams channel."""
    enc_channel = quote(channel_id, safe="")
    enc_name = quote(channel_name or "aidl dashboard", safe="")
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


def _aidl_team_name() -> str:
    return (getattr(settings, "MS_AIDL_TEAM_NAME", None) or "AIDL").strip() or "AIDL"


def _aidl_channel_name() -> str:
    return (
        getattr(settings, "MS_AIDL_CHANNEL_NAME", None) or "aidl dashboard"
    ).strip() or "aidl dashboard"


def list_joined_teams(access_token: str) -> list:
    url = f"{GRAPH_BASE}/me/joinedTeams"
    response = requests.get(url, headers=_graph_headers(access_token), timeout=20)
    response.raise_for_status()
    return response.json().get("value") or []


def list_team_channels(access_token: str, team_id: str) -> list:
    url = f"{GRAPH_BASE}/teams/{team_id}/channels"
    response = requests.get(url, headers=_graph_headers(access_token), timeout=20)
    response.raise_for_status()
    return response.json().get("value") or []


def create_team_channel(
    access_token: str,
    team_id: str,
    display_name: str,
    description: str = "AIDL dashboard — learning and licence updates",
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


def _poll_team_operation(access_token: str, operation_url: str, timeout_sec: int = 90) -> str:
    """
    Team create is async (HTTP 202). Poll until succeeded; return team id if found.
    """
    deadline = time.time() + timeout_sec
    team_id = ""
    while time.time() < deadline:
        resp = requests.get(
            operation_url,
            headers=_graph_headers(access_token),
            timeout=20,
        )
        if resp.status_code == 404:
            time.sleep(2)
            continue
        resp.raise_for_status()
        data = resp.json()
        status = (data.get("status") or "").lower()
        # resourceLocation like https://graph.microsoft.com/v1.0/teams('guid')
        resource = data.get("targetResourceId") or data.get("resourceLocation") or ""
        if "teams(" in resource:
            start = resource.find("teams('") + len("teams('")
            end = resource.find("')", start)
            if end > start:
                team_id = resource[start:end]
        if status in {"succeeded", "success"}:
            return team_id
        if status in {"failed", "failure"}:
            raise RuntimeError(f"Team create operation failed: {data}")
        time.sleep(2)
    raise TimeoutError("Timed out waiting for Microsoft Team creation")


def create_aidl_team(access_token: str, display_name: str) -> str:
    """
    Create a new Microsoft Team named display_name. Returns team (group) id.
    Requires delegated Team.Create (and often org permission to create teams).
    """
    url = f"{GRAPH_BASE}/teams"
    payload = {
        "template@odata.bind": "https://graph.microsoft.com/v1.0/teamsTemplates('standard')",
        "displayName": display_name,
        "description": "AIDL — AI Driving License workspace",
    }
    response = requests.post(
        url,
        headers=_graph_headers(access_token),
        json=payload,
        timeout=30,
    )
    if response.status_code not in (201, 202):
        response.raise_for_status()

    # Prefer Content-Location / Location for team or operation
    location = response.headers.get("Location") or response.headers.get("Content-Location") or ""
    team_id = ""

    if response.status_code == 201:
        data = response.json() if response.content else {}
        team_id = data.get("id") or ""

    if "operations(" in location:
        team_id = _poll_team_operation(access_token, location) or team_id
    elif "teams(" in location and not team_id:
        start = location.find("teams('") + len("teams('")
        end = location.find("')", start)
        if end > start:
            team_id = location[start:end]

    if not team_id:
        # Fallback: re-list joined teams by name (may take a moment)
        time.sleep(3)
        for t in list_joined_teams(access_token):
            if (t.get("displayName") or "").strip().lower() == display_name.lower():
                team_id = t.get("id") or ""
                break

    if not team_id:
        raise RuntimeError("Team created but team id could not be resolved")
    return team_id


def ensure_aidl_team(access_token: str) -> str | None:
    """
    Resolve the AIDL Microsoft Team id:
    1) MS_AIDL_TEAM_ID override if set
    2) Else find joined team named MS_AIDL_TEAM_NAME (default "AIDL")
    3) Else create that team (when MS_AIDL_AUTO_CREATE_TEAM is true)
    """
    override = (getattr(settings, "MS_AIDL_TEAM_ID", None) or "").strip()
    if override:
        return override

    team_name = _aidl_team_name()
    try:
        for t in list_joined_teams(access_token):
            if (t.get("displayName") or "").strip().lower() == team_name.lower():
                return t.get("id") or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("list joined teams failed: %s", exc)

    auto_create = getattr(settings, "MS_AIDL_AUTO_CREATE_TEAM", True)
    if not auto_create:
        return None

    try:
        return create_aidl_team(access_token, team_name)
    except Exception as exc:  # noqa: BLE001
        detail = ""
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            detail = exc.response.text[:500]
        logger.warning("create AIDL team failed: %s %s", exc, detail)
        return None


def ensure_aidl_channel(access_token: str, email: str = "") -> dict | None:
    """
    Ensure Team "AIDL" exists (or MS_AIDL_TEAM_ID), then ensure channel
    "aidl dashboard" inside it. Returns deep-link payload or None on failure.
    Login must not break if this fails.
    """
    if not access_token:
        return None

    try:
        team_id = ensure_aidl_team(access_token)
        if not team_id:
            logger.warning(
                "AIDL team not available (set MS_AIDL_TEAM_ID or enable auto-create + Team.Create)"
            )
            return None

        channel_name = _aidl_channel_name()
        # New teams need a short settle time before channels API works reliably
        channels = []
        for _ in range(5):
            try:
                channels = list_team_channels(access_token, team_id)
                break
            except requests.HTTPError:
                time.sleep(2)

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
            "team_name": _aidl_team_name(),
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
        logger.warning("AIDL team/channel ensure failed: %s %s", exc, detail)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("AIDL team/channel ensure failed: %s", exc)
        return None


def resolve_teams_url(
    *,
    email: str = "",
    ms_access_token: str = "",
    team_id: str = "",
    channel_id: str = "",
    channel_name: str = "",
) -> str:
    """Prefer channel deep link; else platform login_hint fallback."""
    name = (channel_name or _aidl_channel_name()).strip()
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

    if ms_access_token:
        ensured = ensure_aidl_channel(ms_access_token, email=email)
        if ensured and ensured.get("teams_url"):
            return ensured["teams_url"]

    return build_teams_launch_url(email)
