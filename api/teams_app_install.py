"""Install the AIDL Teams app in an organisation's AIDL team, without anyone uploading a zip.

After an admin signs in (and from the hourly dispatch while something is pending), with
that organisation's saved admin login and the delegated permissions in
settings.TEAMS_INSTALL_SCOPES:

1. Catalog: find the app in the company's own app catalog (externalId = TEAMS_APP_ID).
   Not there: publish teams/aidl-app-package.zip. Microsoft lets only Teams / Global
   admins publish straight away; for anyone else it's submitted (?requiresReview=true)
   and their Teams admin approves it in the Teams admin center.
2. Install it in the AIDL team (once published).
3. Add its "AIDL" tab to the aidl dashboard channel.
4. teams_channel.find_app_tab notices the tab: the home card is posted again with
   Open AIDL linking to it, and AIDL's Website tab is removed.

Until step 3 succeeds the card keeps opening AIDL's pages (popup / browser). Every step
checks before it acts, so repeated sign-ins never make a second app, install, tab or card.

Progress: setup_state["teams_app"] = {status, catalog_id, error, at}, status one of
    installed      the tab is in the channel
    submitted      waiting for the company's Teams admin to approve AIDL
    rejected       their Teams admin rejected it
    needs_consent  no admin login with the permissions (sign in again after consent)
    blocked        Microsoft refused (e.g. custom apps are turned off) — error says why
    failed         something else went wrong; tried again later
"""

from __future__ import annotations

import json
import logging
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from .graph_client import GRAPH_BASE, graph_headers
from .models import AIDLUser, Organization, TeamsPost

logger = logging.getLogger(__name__)
PACKAGE = Path(settings.BASE_DIR) / "teams" / "aidl-app-package.zip"
LOCK_KEY = "lock:teams_app_install"
LOCK_FOR = timedelta(minutes=5)
RECHECK = timedelta(hours=1)
TAB_RETRY_WAITS = (4, 8, 15, 0)  # the wait after each try (none after the last)
DONE = ("installed",)


# ---------- progress ----------

def progress(org: Organization) -> dict:
    from .admin_setup import state

    return dict(state(org).get("teams_app") or {})


def _save(org: Organization, status: str, **extra) -> str:
    from .admin_setup import update

    update(org, teams_app={**progress(org), "status": status, "error": "", **extra,
                           "at": timezone.now().isoformat()})
    return status


# ---------- Microsoft Graph ----------

def install_token(org: Organization) -> str:
    """An access token with the install permissions, from a saved admin login
    (the master admin first). "" if none has them yet."""
    from .microsoft_auth import get_access_token_for_user
    from .slack_onboarding import primary_admin

    first = primary_admin(org)
    admins = [first] if first is not None else []
    admins += list(AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True)
                   .exclude(ms_refresh_token="").exclude(pk=getattr(first, "pk", None)))
    for admin in admins:
        token = get_access_token_for_user(admin, settings.TEAMS_INSTALL_SCOPES)
        if token:
            return token
    return ""


def _get(token: str, path: str, params: dict | None = None) -> list | None:
    try:
        resp = requests.get(f"{GRAPH_BASE}{path}", headers=graph_headers(token), params=params, timeout=30)
    except requests.RequestException as exc:
        logger.warning("teams app GET %s failed: %s", path, type(exc).__name__)
        return None
    if resp.status_code >= 400:
        logger.warning("teams app GET %s HTTP %s: %s", path, resp.status_code, (resp.text or "")[:300])
        return None
    return (resp.json() if resp.content else {}).get("value") or []


def _error(resp) -> str:
    try:
        err = resp.json().get("error") or {}
        return f"{resp.status_code} {err.get('code', '')}".strip()[:120]
    except ValueError:
        return str(resp.status_code)


def catalog_app(token: str) -> dict | None:
    """The AIDL app in the company's catalog (with its appDefinitions), or None.
    Raises LookupError if Microsoft wouldn't answer (so we never publish blind)."""
    rows = _get(token, "/appCatalogs/teamsApps",
                {"$filter": f"externalId eq '{settings.TEAMS_APP_ID}' and distributionMethod eq 'organization'",
                 "$expand": "appDefinitions"})
    if rows is None:
        raise LookupError("catalog")
    return rows[0] if rows else None


def publish(token: str) -> tuple[str, dict | None]:
    """("published" | "submitted" | "exists" | "forbidden" | "error:<..>", teamsApp)."""
    data = PACKAGE.read_bytes()
    headers = {**graph_headers(token), "Content-Type": "application/zip"}
    url = f"{GRAPH_BASE}/appCatalogs/teamsApps"
    last = ""
    for review in (False, True):  # admins publish at once; anyone else submits for review
        try:
            resp = requests.post(url + ("?requiresReview=true" if review else ""), headers=headers, data=data,
                                 timeout=60)
        except requests.RequestException as exc:
            return f"error:{type(exc).__name__}", None
        if resp.status_code < 300:
            app = resp.json() if resp.content else {}
            return ("submitted" if review else "published"), app
        if resp.status_code == 409:
            return "exists", None
        last = _error(resp)
        logger.warning("publish AIDL Teams app (review=%s) HTTP %s: %s", review, resp.status_code,
                       (resp.text or "")[:300])
        if resp.status_code not in (401, 403):
            return f"error:{last}", None
    return f"forbidden:{last}", None


def package_version() -> str:
    """The version in teams/aidl-app-package.zip's manifest."""
    with zipfile.ZipFile(PACKAGE) as z:
        return json.loads(z.read("manifest.json").decode("utf-8", "replace").lstrip("﻿"))["version"]


def _vkey(version) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in str(version or "0").split("."))


def update_state(app: dict) -> str:
    """Is the newest AIDL in the company's catalog ours? "" yes (or unknown) · "outdated" an old copy
    (e.g. uploaded before the channel tab existed: Microsoft then says "the app does not support tabs")
    · "submitted" / "rejected" our new version is waiting for / was refused by their Teams admin."""
    defs = [d for d in app.get("appDefinitions") or [] if d.get("version")]
    if not defs:
        return ""
    want = _vkey(package_version())
    mine = [d for d in defs if _vkey(d["version"]) >= want]
    if any(d.get("publishingState") == "published" for d in mine):
        return ""
    states = {d.get("publishingState") for d in mine}
    if "submitted" in states:
        return "submitted"
    if "rejected" in states:
        return "rejected"
    return "outdated"


def publish_update(token: str, catalog_id: str) -> str:
    """Add this package as a new version of the catalog app: "published" | "submitted" |
    "exists" | "forbidden:<..>" | "error:<..>" (same ways as publish)."""
    data = PACKAGE.read_bytes()
    headers = {**graph_headers(token), "Content-Type": "application/zip"}
    url = f"{GRAPH_BASE}/appCatalogs/teamsApps/{catalog_id}/appDefinitions"
    last = ""
    for review in (False, True):
        try:
            resp = requests.post(url + ("?requiresReview=true" if review else ""), headers=headers, data=data,
                                 timeout=60)
        except requests.RequestException as exc:
            return f"error:{type(exc).__name__}"
        if resp.status_code < 300:
            return "submitted" if review else "published"
        if resp.status_code == 409:
            return "exists"
        last = _error(resp)
        logger.warning("update AIDL Teams app (review=%s) HTTP %s: %s", review, resp.status_code,
                       (resp.text or "")[:300])
        if resp.status_code not in (401, 403):
            return f"error:{last}"
    return f"forbidden:{last}"


def upgrade_installed(token: str, team_id: str, catalog_id: str) -> None:
    """The team still runs an older version of the app: move it to the newest."""
    rows = _get(token, f"/teams/{team_id}/installedApps",
                {"$expand": "teamsApp,teamsAppDefinition", "$filter": f"teamsApp/id eq '{catalog_id}'"})
    for row in rows or []:
        have = (row.get("teamsAppDefinition") or {}).get("version")
        if (row.get("teamsApp") or {}).get("id") == catalog_id and row.get("id") and have                 and _vkey(have) < _vkey(package_version()):
            try:
                resp = requests.post(f"{GRAPH_BASE}/teams/{team_id}/installedApps/{row['id']}/upgrade",
                                     headers=graph_headers(token), json={}, timeout=30)
                if resp.status_code >= 400:
                    logger.warning("upgrade AIDL Teams app HTTP %s: %s", resp.status_code, (resp.text or "")[:300])
            except requests.RequestException as exc:
                logger.warning("upgrade AIDL Teams app failed: %s", type(exc).__name__)


def _published(app: dict) -> bool:
    return any(d.get("publishingState") == "published" for d in app.get("appDefinitions") or [])


def _state_of(app: dict) -> str:
    states = {d.get("publishingState") for d in app.get("appDefinitions") or []}
    if "published" in states:
        return "published"
    if "submitted" in states:
        return "submitted"
    if "rejected" in states:
        return "rejected"
    return "submitted"


def is_installed(token: str, team_id: str, catalog_id: str) -> bool | None:
    rows = _get(token, f"/teams/{team_id}/installedApps",
                {"$expand": "teamsApp", "$filter": f"teamsApp/id eq '{catalog_id}'"})
    if rows is None:
        return None
    return any((r.get("teamsApp") or {}).get("id") == catalog_id for r in rows)


def install(token: str, team_id: str, catalog_id: str) -> tuple[bool, str]:
    try:
        resp = requests.post(f"{GRAPH_BASE}/teams/{team_id}/installedApps", headers=graph_headers(token),
                             json={"teamsApp@odata.bind": f"{GRAPH_BASE}/appCatalogs/teamsApps/{catalog_id}"},
                             timeout=30)
    except requests.RequestException as exc:
        return False, type(exc).__name__
    if resp.status_code < 300 or resp.status_code == 409:  # 409: already installed
        return True, ""
    logger.warning("install AIDL Teams app HTTP %s: %s", resp.status_code, (resp.text or "")[:300])
    return False, _error(resp)


# ---------- the whole flow ----------

def _lock(org: Organization) -> bool:
    """One run per organisation at a time (two sign-ins at once must not publish twice)."""
    key = {"organization_id": str(org.pk), "key": LOCK_KEY}
    TeamsPost.objects.filter(**key, created_at__lt=timezone.now() - LOCK_FOR).delete()  # a crashed run
    try:
        TeamsPost.objects.create(**key)
    except IntegrityError:
        return False
    return True


def _unlock(org: Organization) -> None:
    TeamsPost.objects.filter(organization_id=str(org.pk), key=LOCK_KEY).delete()


def enabled(org: Organization | None) -> bool:
    from .teams_channel import channel_id, is_teams

    return bool(settings.TEAMS_AUTO_INSTALL and settings.TEAMS_CHANNEL_TAB and settings.TEAMS_APP_ID
                and is_teams(org) and org.teams_team_id and channel_id(org))


def run(org: Organization) -> str:
    """Do whatever is still missing. Returns the status saved in progress()."""
    if not enabled(org):
        return ""
    if not _lock(org):
        return progress(org).get("status", "")
    try:
        return _run(org)
    except Exception:  # noqa: BLE001 — never break sign-in or the hourly job
        logger.exception("AIDL Teams app install failed for org %s", org.pk)
        return _save(org, "failed", error="unexpected")
    finally:
        _unlock(org)


def _run(org: Organization) -> str:
    from . import teams_channel
    from . import teams_channel_tabs as tabs

    team, channel = org.teams_team_id, teams_channel.channel_id(org)
    token = install_token(org)
    if not token:
        return _save(org, "needs_consent")

    # The tab may already be there (added before, or by hand): then only the card link is missing.
    if tabs.find_tab(token, team, channel, tabs.CUSTOM_TAB_ENTITY) is not None:
        return _finish(org)

    # 1. the company's app catalog
    try:
        app = catalog_app(token)
    except LookupError:
        return _save(org, "failed", error="catalog_unreadable")
    if app is None:
        outcome, app = publish(token)
        if outcome == "exists":  # someone else just published it
            try:
                app = catalog_app(token)
            except LookupError:
                app = None
            if app is None:
                return _save(org, "failed", error="catalog_conflict")
        elif outcome == "submitted":
            return _save(org, "submitted", catalog_id=(app or {}).get("id", ""))
        elif outcome != "published":
            status = "blocked" if outcome.startswith("forbidden") else "failed"
            return _save(org, status, error=outcome.split(":", 1)[-1])
        else:
            app = {**(app or {}), "appDefinitions": [{"publishingState": "published"}]}
    catalog_id = (app or {}).get("id", "")
    state = _state_of(app)
    if state != "published":
        return _save(org, state, catalog_id=catalog_id)
    behind = update_state(app)
    if behind in ("submitted", "rejected"):  # the new version waits for their Teams admin (or was refused)
        return _save(org, behind, catalog_id=catalog_id)
    if behind == "outdated":  # an old copy in the catalog has no channel tab
        outcome = publish_update(token, catalog_id)
        if outcome == "submitted":
            return _save(org, "submitted", catalog_id=catalog_id)
        if outcome not in ("published", "exists"):
            return _save(org, "blocked" if outcome.startswith("forbidden") else "failed", catalog_id=catalog_id,
                         error=f"update {outcome.split(':', 1)[-1]}")

    # 2. installed in the AIDL team
    installed = is_installed(token, team, catalog_id)
    if installed is None:
        return _save(org, "failed", catalog_id=catalog_id, error="installed_apps_unreadable")
    if not installed:
        ok, error = install(token, team, catalog_id)
        if not ok:
            return _save(org, "blocked" if error.startswith("403") else "failed", catalog_id=catalog_id, error=error)
    else:
        upgrade_installed(token, team, catalog_id)

    # 3. the channel tab
    # A just-installed app isn't always ready for a tab for a few seconds (400 / 404): try again.
    for wait in TAB_RETRY_WAITS:
        added = tabs.add_app_tab(token, team_id=team, channel_id=channel, catalog_app_id=catalog_id,
                                 content_url=teams_channel.app_url("tab/"))
        if added["ok"] or added["status"] not in (400, 404):
            break
        time.sleep(wait)
    if not added["ok"] and tabs.find_tab(token, team, channel, tabs.CUSTOM_TAB_ENTITY) is None:
        return _save(org, "blocked" if added["status"] == 403 else "failed", catalog_id=catalog_id,
                     error=f"tab {added['status']} {added.get('detail', '')}".strip())
    return _finish(org, catalog_id=catalog_id)


def _finish(org: Organization, **extra) -> str:
    """The tab is there: point Open AIDL at it (posts the card again only if the link is new)."""
    from . import teams_channel

    if not teams_channel.app_tab_link(org) and teams_channel.find_app_tab(org):
        teams_channel.publish_home(org)
    return _save(org, "installed", **extra)


CHECK_EVERY = timedelta(seconds=45)


def check_now(org: Organization) -> str:
    """The Admin Center asks every ~30 s while AIDL waits for approval: look again (at most
    every 45 s) and, once approved, finish the install. Returns the current status."""
    p = progress(org)
    if p.get("status") != "submitted" or not enabled(org):
        return p.get("status", "")
    try:
        recent = timezone.now() - datetime.fromisoformat(p["at"]) < CHECK_EVERY
    except (KeyError, ValueError):
        recent = False
    return p["status"] if recent else (run(org) or p["status"])


def due(org: Organization) -> bool:
    """The hourly dispatch tries again, at most hourly, until it's installed. A rejection
    waits for someone to sign in again (their admin may have changed their mind)."""
    p = progress(org)
    if p.get("status") in DONE + ("rejected",):
        return False
    try:
        return not p.get("at") or timezone.now() - datetime.fromisoformat(p["at"]) >= RECHECK
    except ValueError:
        return True


def run_if_due(org: Organization) -> None:
    if enabled(org) and due(org):
        run(org)
