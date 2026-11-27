"""The two Google APIs the Gmail side panel calls for the admin, with the person's own access
(Google sends it with every side-panel request, for the scopes in addon_deployment.json):

    directory(event, domain)          everyone in the company's Google directory (Admin SDK,
                                      admin.directory.user.readonly, domain-public view)
    attachments(event) / attachment() the files on the email that is open in Gmail
                                      (gmail.addons.current.message.readonly)
"""

from __future__ import annotations

import base64
import logging

import requests

logger = logging.getLogger(__name__)

DIRECTORY = "https://admin.googleapis.com/admin/directory/v1/users"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
TIMEOUT = 15
MAX_PEOPLE = 2000
POLICY_FILES = ("pdf", "docx", "txt", "md")


class GoogleApiError(Exception):
    pass


def _token(event: dict) -> str:
    token = ((event or {}).get("authorizationEventObject") or {}).get("userOAuthToken", "")
    if not token:
        raise GoogleApiError("Google didn't give AIDL access. Reinstall the add-on and allow access.")
    return token


def _get(url: str, headers: dict, params: dict | None = None) -> dict:
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise GoogleApiError("Couldn't reach Google. Please try again.") from exc
    if resp.status_code in (401, 403):
        logger.warning("google api %s refused: %s %s", url, resp.status_code, resp.text[:300])
        if "insufficient" in resp.text.lower() and "scope" in resp.text.lower():
            raise GoogleApiError("scope")  # the installed add-on doesn't ask for this permission yet
        raise GoogleApiError("refused")
    if resp.status_code >= 300:
        logger.warning("google api %s: %s %s", url, resp.status_code, resp.text[:300])
        raise GoogleApiError("Google couldn't answer right now. Please try again.")
    return resp.json()


# ---------- the company directory ----------

def directory(event: dict, domain: str) -> list[dict]:
    """[{email, name}] for everyone in the domain, sorted by email. GoogleApiError("refused") when the
    company's Google settings don't let this person see the directory."""
    try:
        return _directory(event, domain, "domain_public")
    except GoogleApiError as exc:
        if str(exc) != "refused":
            raise
        # Contact sharing is off for the company: a Google Workspace admin can still read it with admin rights.
        return _directory(event, domain, "admin_view")


def _directory(event: dict, domain: str, view: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {_token(event)}"}
    people, page = [], ""
    while len(people) < MAX_PEOPLE:
        params = {"domain": domain, "viewType": view, "maxResults": 500, "orderBy": "email"}
        if page:
            params["pageToken"] = page
        try:
            data = _get(DIRECTORY, headers, params)
        except GoogleApiError as exc:
            if str(exc) == "scope":  # say which permissions the token does have (for setup problems)
                try:
                    info = requests.get("https://oauth2.googleapis.com/tokeninfo",
                                        params={"access_token": _token(event)}, timeout=TIMEOUT).json()
                    logger.warning("add-on token scopes: %s", info.get("scope"))
                except Exception:  # noqa: BLE001 - only a log line
                    pass
            raise
        for u in data.get("users") or []:
            email = (u.get("primaryEmail") or "").lower()
            if email and not u.get("suspended") and not u.get("archived"):
                people.append({"email": email, "name": ((u.get("name") or {}).get("fullName") or "").strip()})
        page = data.get("nextPageToken") or ""
        if not page:
            break
    return people[:MAX_PEOPLE]


# ---------- the open email's attachments ----------

def _gmail_headers(event: dict) -> dict:
    gmail = (event or {}).get("gmail") or {}
    if not gmail.get("messageId") or not gmail.get("accessToken"):
        raise GoogleApiError("Open the email with your policy file first.")
    return {"Authorization": f"Bearer {_token(event)}", "X-Goog-Gmail-Access-Token": gmail["accessToken"]}


def _parts(part: dict):
    yield part
    for p in part.get("parts") or []:
        yield from _parts(p)


def attachments(event: dict) -> list[dict]:
    """[{id, name, size}] of the policy-like files (PDF, Word, text) on the open email."""
    headers = _gmail_headers(event)
    msg = _get(f"{GMAIL}/{event['gmail']['messageId']}", headers, {"format": "full"})
    out = []
    for p in _parts(msg.get("payload") or {}):
        name = p.get("filename") or ""
        att = (p.get("body") or {}).get("attachmentId")
        if att and name.rsplit(".", 1)[-1].lower() in POLICY_FILES:
            out.append({"id": att, "name": name, "size": int((p.get("body") or {}).get("size") or 0)})
    return out


def attachment(event: dict, attachment_id: str) -> bytes:
    headers = _gmail_headers(event)
    data = _get(f"{GMAIL}/{event['gmail']['messageId']}/attachments/{attachment_id}", headers)
    return base64.urlsafe_b64decode((data.get("data") or "") + "==")
