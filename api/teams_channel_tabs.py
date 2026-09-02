"""Install AIDL UI tabs on the Microsoft Teams channel + build tab deep links."""

import logging

import requests
from django.conf import settings

from .microsoft_auth import GRAPH_BASE, _graph_headers
from .teams_cards import TEAMS_TABS


logger = logging.getLogger(__name__)

# Built-in Teams "Website" tab — works without uploading a custom app manifest.
WEBSITE_TAB_APP_ID = "com.microsoft.teamspace.tab.web"

AIDL_CHANNEL_TABS = tuple(
    (f"aidl-{tab_id}", label, tab_id)
    for tab_id, label, _icon in TEAMS_TABS
)


def _teams_tab_base_url() -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://aidl-backend.onrender.com/api/teams"


def _tab_content_url(tab_slug: str) -> str:
    return f"{_teams_tab_base_url()}/tabs/{tab_slug}/"


def list_channel_tabs(access_token: str, team_id: str, channel_id: str) -> list:
    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/tabs"
    response = requests.get(url, headers=_graph_headers(access_token), timeout=20)
    response.raise_for_status()
    return response.json().get("value") or []


def create_channel_website_tab(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    entity_id: str,
    display_name: str,
    content_url: str,
) -> dict | None:
    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/tabs"
    payload = {
        "displayName": display_name,
        "teamsApp@odata.bind": (
            f"https://graph.microsoft.com/v1.0/appCatalogs/teamsApps('{WEBSITE_TAB_APP_ID}')"
        ),
        "configuration": {
            "entityId": entity_id,
            "contentUrl": content_url,
            "websiteUrl": content_url,
            "removeUrl": None,
        },
    }
    try:
        response = requests.post(
            url,
            headers=_graph_headers(access_token),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.text[:500] if exc.response is not None else ""
        except Exception:  # noqa: BLE001
            detail = str(exc)
        logger.warning(
            "create channel tab %s failed: %s %s",
            display_name,
            exc,
            detail,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("create channel tab %s failed: %s", display_name, exc)
        return None


def _existing_entity_ids(existing_tabs: list) -> set[str]:
    ids: set[str] = set()
    for tab in existing_tabs:
        config = tab.get("configuration") or {}
        entity_id = (config.get("entityId") or "").strip()
        if entity_id:
            ids.add(entity_id)
    return ids


def ensure_aidl_channel_tabs(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
) -> dict | None:
    """
    Add Home / Learner's Permit / Highway Code / Traffic Light Check tabs
    to the AIDL channel. Safe to call on every login (skips existing tabs).
    """
    if not getattr(settings, "MS_AIDL_INSTALL_CHANNEL_TABS", True):
        return None
    if not access_token or not team_id or not channel_id:
        return None

    try:
        existing_tabs = list_channel_tabs(access_token, team_id, channel_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("list channel tabs failed: %s", exc)
        existing_tabs = []

    present = _existing_entity_ids(existing_tabs)
    created = []
    for entity_id, display_name, tab_slug in AIDL_CHANNEL_TABS:
        if entity_id in present:
            continue
        result = create_channel_website_tab(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            entity_id=entity_id,
            display_name=display_name,
            content_url=_tab_content_url(tab_slug),
        )
        if result:
            created.append(display_name)

    home_entity = AIDL_CHANNEL_TABS[0][0]
    return {
        "home_entity_id": home_entity,
        "tabs_created": created,
        "tabs_present": len(present) + len(created),
    }


def build_channel_tab_deep_link(
    *,
    entity_id: str,
    team_id: str,
    channel_id: str,
    tenant_id: str = "",
    email: str = "",
    app_id: str = "",
) -> str:
    """
    Deep link that opens a specific tab inside the AIDL channel
    (Home tab shows the welcome card + in-page menu).
    """
    from urllib.parse import quote

    app = (app_id or WEBSITE_TAB_APP_ID).strip()
    tenant = (tenant_id or settings.MS_TENANT_ID or "").strip()
    url = (
        f"https://teams.microsoft.com/l/entity/{quote(app, safe='')}/{quote(entity_id, safe='')}"
        f"?groupId={quote(team_id, safe='')}"
        f"&channelId={quote(channel_id, safe='')}"
    )
    if tenant and tenant.lower() != "common":
        url += f"&tenantId={quote(tenant, safe='')}"
    email = (email or "").strip()
    if email:
        url += f"&login_hint={quote(email)}"
    return url


def build_home_tab_deep_link(
    *,
    team_id: str,
    channel_id: str,
    tenant_id: str = "",
    email: str = "",
) -> str:
    return build_channel_tab_deep_link(
        entity_id=AIDL_CHANNEL_TABS[0][0],
        team_id=team_id,
        channel_id=channel_id,
        tenant_id=tenant_id,
        email=email,
    )


def is_aidl_teams_landing_url(url: str) -> bool:
    value = (url or "").strip()
    if not value:
        return False
    return "/l/channel/" in value or "/l/entity/" in value
