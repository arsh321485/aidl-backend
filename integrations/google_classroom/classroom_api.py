"""The two Google Classroom API calls AIDL needs (read only).

https://developers.google.com/classroom/reference/rest
"""

from __future__ import annotations

import logging

import requests

from .google_oauth import GoogleError

logger = logging.getLogger(__name__)

API = "https://classroom.googleapis.com/v1"
PAGE_SIZE = 100
MAX_PAGES = 50  # 5,000 items: far more than any one teacher has


def list_courses(access_token: str) -> list[dict]:
    """Active classes the signed-in person teaches."""
    return _all_pages(access_token, f"{API}/courses", "courses",
                      {"teacherId": "me", "courseStates": "ACTIVE"})


def list_students(access_token: str, course_id: str) -> list[dict]:
    return _all_pages(access_token, f"{API}/courses/{course_id}/students", "students", {})


def _all_pages(access_token: str, url: str, key: str, params: dict) -> list[dict]:
    items, token = [], ""
    for _ in range(MAX_PAGES):
        query = {**params, "pageSize": PAGE_SIZE}
        if token:
            query["pageToken"] = token
        data = _get(access_token, url, query)
        items.extend(data.get(key) or [])
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return items


def _get(access_token: str, url: str, params: dict) -> dict:
    try:
        resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("classroom api unreachable: %s", exc)
        raise GoogleError("classroom_unreachable", "Could not reach Google Classroom. Please try again.") from exc
    if resp.ok:
        return resp.json()

    try:
        error = resp.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") or ""
    logger.warning("classroom api %s %s: %s", resp.status_code, error.get("status"), message)
    if resp.status_code == 401:
        raise GoogleError("reconnect", "Google Classroom access has expired. Please connect again.")
    if resp.status_code == 403 and ("has not been used" in message or "is disabled" in message
                                    or "SERVICE_DISABLED" in str(error)):
        raise GoogleError("classroom_api_disabled",
                          "The Google Classroom API is switched off for AIDL's Google Cloud project. "
                          "Turn it on in Google Cloud → APIs & Services → Library → Google Classroom API.")
    if resp.status_code == 403:
        raise GoogleError("classroom_forbidden",
                          "Google Classroom didn't allow AIDL to read your classes. If your school manages "
                          "your Google account, your Workspace admin may need to allow AIDL.")
    raise GoogleError("classroom_error", "Google Classroom returned an error. Please try again.")
