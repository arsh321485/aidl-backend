"""The Google Workspace Admin SDK (Directory API) calls for "whole school" (school_directory.py).

Only a school's Workspace admin can use these: Google answers 403 to anyone else,
which is how AIDL tells an IT admin from a teacher.

https://developers.google.com/admin-sdk/directory/reference/rest
"""

from __future__ import annotations

import logging

import requests

from .google_oauth import GoogleError

logger = logging.getLogger(__name__)

API = "https://admin.googleapis.com/admin/directory/v1"
USERS_PAGE = 500  # Google's largest page
MAX_PAGES = 200  # 100,000 accounts


def admin_flags(access_token: str, email: str) -> dict:
    """{"isAdmin": bool, "isDelegatedAdmin": bool} for this account. Raises
    GoogleError("not_admin") when the person isn't a Workspace admin."""
    return _get(access_token, f"{API}/users/{email}",
                {"viewType": "admin_view", "projection": "basic", "fields": "primaryEmail,isAdmin,isDelegatedAdmin"})


def list_org_units(access_token: str) -> list[dict]:
    """Every organisational unit ("folder") of the school: name, orgUnitPath, description."""
    data = _get(access_token, f"{API}/customer/my_customer/orgunits",
                {"type": "all", "fields": "organizationUnits(name,orgUnitPath,description)"})
    return data.get("organizationUnits") or []


def list_users_in(access_token: str, org_unit_path: str) -> list[dict]:
    """The accounts in this organisational unit and the units under it:
    id, primaryEmail, name.fullName, suspended, archived."""
    path = org_unit_path.replace("\\", "\\\\").replace("'", "\\'")
    params = {"customer": "my_customer", "query": f"orgUnitPath='{path}'", "maxResults": USERS_PAGE,
              "projection": "basic", "viewType": "admin_view",
              "fields": "nextPageToken,users(id,primaryEmail,name/fullName,suspended,archived)"}
    users, token = [], ""
    for _ in range(MAX_PAGES):
        data = _get(access_token, f"{API}/users", {**params, **({"pageToken": token} if token else {})})
        users.extend(data.get("users") or [])
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return users


def _get(access_token: str, url: str, params: dict) -> dict:
    try:
        resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        logger.warning("directory api unreachable: %s", exc)
        raise GoogleError("directory_unreachable", "Could not reach Google. Please try again.") from exc
    if resp.ok:
        return resp.json()
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") or ""
    logger.warning("directory api %s: %s", resp.status_code, message)
    if resp.status_code == 401:
        raise GoogleError("reconnect", "Google access for the whole school has expired. Your IT admin needs to "
                                       "connect again.")
    if resp.status_code == 403 and ("has not been used" in message or "is disabled" in message
                                    or "SERVICE_DISABLED" in str(error)):
        raise GoogleError("directory_api_disabled",
                          "The Admin SDK API is switched off for AIDL's Google Cloud project. Turn it on in "
                          "Google Cloud → APIs & Services → Library → Admin SDK API.")
    if resp.status_code in (403, 404):  # a non-admin gets 403 (sometimes 404 for their own account)
        raise GoogleError("not_admin", "This Google account isn't an admin of your school's Google Workspace. "
                                       "Only your IT admin can connect the whole school.")
    raise GoogleError("directory_error", "Google returned an error while reading your school's accounts. "
                                         "Please try again.")
