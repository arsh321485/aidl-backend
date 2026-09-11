"""Install AIDL UI tabs on the Microsoft Teams channel + build tab deep links."""

import logging
import time

import requests
from django.conf import settings

from .graph_client import GRAPH_BASE, graph_headers
from .teams_admin import ADMIN_TABS


logger = logging.getLogger(__name__)

# Built-in Teams "Website" tab — works without uploading a custom app manifest.
WEBSITE_TAB_APP_ID = "com.microsoft.teamspace.tab.web"

# Channel tabs = the same 6 pills as the in-page Admin Center header
# (Home, Add Admin, Policy, Cards, AI Apps, IT Apps) so every tab a user opens
# in Teams renders admin.html with a consistent, clickable nav.
AIDL_CHANNEL_TABS = tuple(
    (f"aidl-{tab_id}", label, tab_id)
    for tab_id, label, _icon in ADMIN_TABS
)
_AIDL_ENTITY_PREFIX = "aidl-"


def target_channel_name() -> str:
    return (
        getattr(settings, "MS_AIDL_CHANNEL_NAME", None) or "aidl dashboard"
    ).strip().lower() or "aidl dashboard"


def is_aidl_dashboard_channel(channel_name: str) -> bool:
    """Tabs must only be installed on aidl dashboard — never General."""
    name = (channel_name or "").strip().lower()
    if not name or name == "general":
        return False
    return name == target_channel_name()


def _teams_tab_base_url() -> str:
    configured = (getattr(settings, "MS_TEAMS_APP_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://aidl-backend.onrender.com/api/teams"


def _tab_content_url(tab_slug: str) -> str:
    return f"{_teams_tab_base_url()}/tabs/{tab_slug}/"


def list_channel_tabs(access_token: str, team_id: str, channel_id: str) -> list:
    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/tabs"
    response = requests.get(url, headers=graph_headers(access_token), timeout=20)
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
) -> tuple[dict | None, str | None]:
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
        },
    }
    try:
        response = requests.post(
            url,
            headers=graph_headers(access_token),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json(), None
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.text[:500] if exc.response is not None else ""
        except Exception:  # noqa: BLE001
            detail = str(exc)
        logger.warning(
            "create channel tab %s on channel %s failed: %s %s",
            display_name,
            channel_id,
            exc,
            detail,
        )
        return None, detail or str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("create channel tab %s failed: %s", display_name, exc)
        return None, str(exc)


def delete_channel_tab(access_token: str, team_id: str, channel_id: str, tab_id: str) -> bool:
    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/tabs/{tab_id}"
    try:
        response = requests.delete(url, headers=graph_headers(access_token), timeout=20)
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete channel tab %s on channel %s failed: %s", tab_id, channel_id, exc)
        return False


def _existing_entity_ids(existing_tabs: list) -> set[str]:
    ids: set[str] = set()
    for tab in existing_tabs:
        config = tab.get("configuration") or {}
        entity_id = (config.get("entityId") or "").strip()
        if entity_id:
            ids.add(entity_id)
    return ids


def _stale_aidl_tabs(existing_tabs: list, expected_entity_ids: set[str]) -> list[dict]:
    """
    Tabs from a previous AIDL tab set (e.g. the old Learner's Permit / Highway
    Code / Traffic Light Check tabs) that are no longer part of the current
    6-pill Admin Center header. Identified by our "aidl-" entity id prefix so
    we never touch tabs a customer added themselves.
    """
    stale = []
    for tab in existing_tabs:
        config = tab.get("configuration") or {}
        entity_id = (config.get("entityId") or "").strip()
        tab_id = (tab.get("id") or "").strip()
        if not tab_id or not entity_id.startswith(_AIDL_ENTITY_PREFIX):
            continue
        if entity_id in expected_entity_ids:
            continue
        stale.append({"id": tab_id, "entity_id": entity_id, "display_name": tab.get("displayName") or entity_id})
    return stale


def _find_home_tab_web_url(tabs: list) -> str:
    """Prefer Microsoft Graph's official Home tab webUrl for reliable landing."""
    home_entity = AIDL_CHANNEL_TABS[0][0]
    for tab in tabs:
        config = tab.get("configuration") or {}
        entity_id = (config.get("entityId") or "").strip()
        name = (tab.get("displayName") or "").strip().lower()
        if entity_id == home_entity or name == "home":
            web_url = (tab.get("webUrl") or "").strip()
            if web_url:
                return web_url
    return ""


def ensure_aidl_channel_tabs(
    access_token: str,
    *,
    team_id: str,
    channel_id: str,
    channel_name: str = "",
    channel_just_created: bool = False,
) -> dict | None:
    """
    Add the Admin Center tabs — Home / Add Admin / Policy / Cards / AI Apps /
    IT Apps — ONLY on the configured aidl dashboard channel (never General),
    and remove any stale tabs from a previous AIDL tab set so every tab a
    user opens shows the same clickable pill header.
    """
    if not getattr(settings, "MS_AIDL_INSTALL_CHANNEL_TABS", True):
        return {"skipped": True, "reason": "disabled"}

    if not access_token or not team_id or not channel_id:
        return {"skipped": True, "reason": "missing_ids"}

    if not is_aidl_dashboard_channel(channel_name):
        logger.warning(
            "Refusing tab install on channel %r — tabs only go on %r",
            channel_name,
            target_channel_name(),
        )
        return {
            "skipped": True,
            "reason": "wrong_channel",
            "channel_name": channel_name,
            "expected_channel": target_channel_name(),
        }

    if channel_just_created:
        time.sleep(5)

    existing_tabs: list = []
    last_list_error = ""
    for attempt in range(5):
        try:
            existing_tabs = list_channel_tabs(access_token, team_id, channel_id)
            break
        except Exception as exc:  # noqa: BLE001
            last_list_error = str(exc)
            logger.warning(
                "list channel tabs attempt %s/5 failed for %s: %s",
                attempt + 1,
                channel_id,
                exc,
            )
            time.sleep(3)
    else:
        return {
            "ok": False,
            "channel_name": channel_name,
            "error": last_list_error or "could_not_list_tabs",
        }

    present = _existing_entity_ids(existing_tabs)
    created: list[str] = []
    failed: list[dict] = []
    home_web_url = _find_home_tab_web_url(existing_tabs)

    expected_entity_ids = {entity_id for entity_id, _label, _slug in AIDL_CHANNEL_TABS}
    removed: list[str] = []
    for stale in _stale_aidl_tabs(existing_tabs, expected_entity_ids):
        if delete_channel_tab(access_token, team_id, channel_id, stale["id"]):
            removed.append(stale["display_name"])
            present.discard(stale["entity_id"])

    for entity_id, display_name, tab_slug in AIDL_CHANNEL_TABS:
        if entity_id in present:
            continue
        result, error = create_channel_website_tab(
            access_token,
            team_id=team_id,
            channel_id=channel_id,
            entity_id=entity_id,
            display_name=display_name,
            content_url=_tab_content_url(tab_slug),
        )
        if result:
            created.append(display_name)
            if entity_id == AIDL_CHANNEL_TABS[0][0]:
                home_web_url = (result.get("webUrl") or "").strip() or home_web_url
        else:
            failed.append({"tab": display_name, "error": error or "unknown"})

    # Re-list so we pick up Graph's canonical Home webUrl after creates.
    if created or not home_web_url:
        try:
            refreshed = list_channel_tabs(access_token, team_id, channel_id)
            home_web_url = _find_home_tab_web_url(refreshed) or home_web_url
            present = _existing_entity_ids(refreshed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh channel tabs after create failed: %s", exc)

    home_entity = AIDL_CHANNEL_TABS[0][0]
    return {
        "ok": len(failed) == 0,
        "channel_name": channel_name,
        "channel_id": channel_id,
        "home_entity_id": home_entity,
        "home_web_url": home_web_url,
        "tabs_created": created,
        "tabs_removed": removed,
        "tabs_failed": failed,
        "tabs_present": len(present) + len(created),
        "expected_tabs": len(AIDL_CHANNEL_TABS),
    }


def build_channel_tab_deep_link(
    *,
    entity_id: str,
    team_id: str,
    channel_id: str,
    tenant_id: str = "",
    email: str = "",
    app_id: str = "",
    label: str = "Home",
) -> str:
    """
    Deep link that opens a specific tab inside the AIDL channel
    (Home tab shows the welcome card + in-page menu).

    Uses Teams entity deep-link + context JSON so the channel opens on Home,
    not the Posts/chat tab.
    """
    import json
    from urllib.parse import quote

    app = (app_id or WEBSITE_TAB_APP_ID).strip()
    tenant = (tenant_id or settings.MS_TENANT_ID or "").strip()
    context = {
        "channelId": channel_id,
        "groupId": team_id,
    }
    if tenant and tenant.lower() != "common":
        context["tenantId"] = tenant

    url = (
        f"https://teams.microsoft.com/l/entity/{quote(app, safe='')}/{quote(entity_id, safe='')}"
        f"?label={quote(label or 'Home', safe='')}"
        f"&context={quote(json.dumps(context, separators=(',', ':')), safe='')}"
    )
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
    home_web_url: str = "",
) -> str:
    """Prefer Graph Home tab webUrl; else build entity deep link with context."""
    graph_url = (home_web_url or "").strip()
    if graph_url:
        email = (email or "").strip()
        if email and "login_hint=" not in graph_url:
            sep = "&" if "?" in graph_url else "?"
            from urllib.parse import quote

            return f"{graph_url}{sep}login_hint={quote(email)}"
        return graph_url

    return build_channel_tab_deep_link(
        entity_id=AIDL_CHANNEL_TABS[0][0],
        team_id=team_id,
        channel_id=channel_id,
        tenant_id=tenant_id,
        email=email,
        label="Home",
    )


def is_aidl_teams_landing_url(url: str) -> bool:
    value = (url or "").strip()
    if not value:
        return False
    return "/l/channel/" in value or "/l/entity/" in value
