"""
Publish the AIDL Teams app (teams/manifest.json) to the org's app catalog
and install it on the AIDL team — required for Adaptive Card Action.Execute
(in-card nav pill clicks) to route to our bot instead of showing Teams'
"That action isn't supported here."

Graph-posted cards carry no app/bot context by themselves; the app has to
be an actual installed app on the team before Teams will route an invoke.
"""

from __future__ import annotations

import io
import logging
import zipfile

import requests
from django.conf import settings

from .graph_client import GRAPH_BASE, graph_headers


logger = logging.getLogger(__name__)

MANIFEST_DIR = settings.BASE_DIR / "teams"


def _app_id() -> str:
    return (getattr(settings, "MS_BOT_APP_ID", None) or settings.MS_CLIENT_ID or "").strip()


def _build_app_package() -> bytes:
    """Zip teams/manifest.json + color.png + outline.png in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("manifest.json", "color.png", "outline.png"):
            path = MANIFEST_DIR / name
            zf.write(path, arcname=name)
    return buf.getvalue()


def _find_catalog_app_id(access_token: str) -> str | None:
    app_id = _app_id()
    if not app_id:
        return None
    url = (
        f"{GRAPH_BASE}/appCatalogs/teamsApps"
        f"?$filter=externalId eq '{app_id}'"
    )
    response = requests.get(url, headers=graph_headers(access_token), timeout=20)
    response.raise_for_status()
    values = response.json().get("value") or []
    if values:
        return values[0].get("id")
    return None


def _upload_app_to_catalog(access_token: str) -> str:
    url = f"{GRAPH_BASE}/appCatalogs/teamsApps"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/zip",
    }
    response = requests.post(url, headers=headers, data=_build_app_package(), timeout=30)
    response.raise_for_status()
    data = response.json() if response.content else {}
    catalog_id = data.get("id") or ""
    if not catalog_id:
        # Some tenants return 202 + Location instead of a body.
        location = response.headers.get("Location") or ""
        if "teamsApps(" in location:
            start = location.find("teamsApps('") + len("teamsApps('")
            end = location.find("')", start)
            if end > start:
                catalog_id = location[start:end]
    if not catalog_id:
        raise RuntimeError("app uploaded but catalog id could not be resolved")
    return catalog_id


def _is_app_installed(access_token: str, team_id: str, catalog_app_id: str) -> bool:
    url = f"{GRAPH_BASE}/teams/{team_id}/installedApps?$expand=teamsApp"
    response = requests.get(url, headers=graph_headers(access_token), timeout=20)
    response.raise_for_status()
    for item in response.json().get("value") or []:
        app = item.get("teamsApp") or {}
        if app.get("id") == catalog_app_id:
            return True
    return False


def _install_app_on_team(access_token: str, team_id: str, catalog_app_id: str) -> None:
    url = f"{GRAPH_BASE}/teams/{team_id}/installedApps"
    payload = {
        "teamsApp@odata.bind": f"{GRAPH_BASE}/appCatalogs/teamsApps('{catalog_app_id}')"
    }
    response = requests.post(
        url, headers=graph_headers(access_token), json=payload, timeout=30
    )
    response.raise_for_status()


def ensure_aidl_app_installed(access_token: str, *, team_id: str) -> dict:
    """
    Best-effort: publish the AIDL app to the tenant catalog (once) and
    install it on the given team (once). Never raises — login must not
    break if this fails (e.g. tenant blocks custom app uploads).
    """
    if not getattr(settings, "MS_AIDL_INSTALL_APP", True):
        return {"skipped": True, "reason": "disabled"}
    if not access_token or not team_id:
        return {"skipped": True, "reason": "missing_ids"}

    try:
        catalog_app_id = _find_catalog_app_id(access_token)
        if not catalog_app_id:
            catalog_app_id = _upload_app_to_catalog(access_token)

        if _is_app_installed(access_token, team_id, catalog_app_id):
            return {"ok": True, "catalog_app_id": catalog_app_id, "already_installed": True}

        _install_app_on_team(access_token, team_id, catalog_app_id)
        return {"ok": True, "catalog_app_id": catalog_app_id, "already_installed": False}
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.text[:500] if exc.response is not None else ""
        except Exception:  # noqa: BLE001
            detail = str(exc)
        logger.warning("ensure AIDL app installed failed: %s %s", exc, detail)
        return {"ok": False, "error": detail or str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure AIDL app installed failed: %s", exc)
        return {"ok": False, "error": str(exc)}
