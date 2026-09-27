"""Slack interactivity endpoint — button clicks on the Admin cards in #aidl.

Slack app → Interactivity & Shortcuts → Request URL must point here
(<backend>/api/slack/interactions/). Every tab click answers with an
ephemeral copy of that card, visible only to the admin who clicked, so
admins can browse the Admin Center without changing the shared message.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

import requests
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .auth_jwt import create_access_token
from .models import AIDLUser
from . import slack_client, slack_modals
from .org_service import get_organization_for_user
from .slack_blocks import admin_card_blocks, admin_card_text, publish_admin_center
from .slack_cards import build_admin_cards_context

logger = logging.getLogger(__name__)


def verify_slack_signature(request) -> bool:
    secret = settings.SLACK_SIGNING_SECRET
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not (secret and timestamp.isdigit() and signature):
        return False
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False  # replay protection
    base = f"v0:{timestamp}:".encode() + request.body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _post_reply(response_url: str, body: dict) -> None:
    try:
        resp = requests.post(response_url, json=body, timeout=10)
        if resp.status_code != 200:
            logger.warning("slack response_url -> %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack response_url post failed: %s", exc)


def _reply(response_url: str, *, text: str, blocks: list | None = None, replace: bool = False) -> None:
    """Send the ephemeral card through response_url in the background, so the
    click is acknowledged well inside Slack's 3-second limit (a late answer
    is what makes Slack show the warning icon next to the button)."""
    body = {"response_type": "ephemeral", "replace_original": replace, "text": text}
    if blocks:
        body["blocks"] = blocks
    if not response_url:
        return
    if settings.SLACK_REPLY_SYNC:
        _post_reply(response_url, body)
    else:
        threading.Thread(target=_post_reply, args=(response_url, body), daemon=True).start()


def _private_link(request, user: AIDLUser, tab: str) -> str:
    base = request.build_absolute_uri("/").rstrip("/")
    token = create_access_token(user)
    if tab == "csv":
        return f"{base}/api/slack/cards/admin/coverage.csv?token={token}"
    if tab == "home":
        return f"{base}/api/slack/cards/admin/coverage/?token={token}"
    return f"{base}/api/slack/cards/admin/?token={token}#{tab}"


def _admin_for(payload: dict) -> AIDLUser | None:
    team_id = (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id", "")
    slack_user = (payload.get("user") or {}).get("id", "")
    return AIDLUser.objects.filter(microsoft_id=f"slack:{team_id}:{slack_user}", is_active=True).first()


def _open_modal(org, trigger_id: str, view: dict, *, push: bool = False) -> str:
    """Opens (or pushes) a modal; returns its view id."""
    method = "views.push" if push else "views.open"
    result = slack_client.slack_api(method, slack_client.bot_token(org), json={"trigger_id": trigger_id, "view": view})
    if not result.get("ok"):
        logger.warning("slack %s failed: %s %s", method, result.get("error"), result.get("response_metadata"))
    return (result.get("view") or {}).get("id", "")


def _notify(org, channel_id: str, slack_user: str, text: str, admin: AIDLUser) -> None:
    """After a modal is submitted: a private confirmation in the channel and a
    refreshed Home card (the numbers changed)."""
    token = slack_client.bot_token(org)
    if channel_id:
        slack_client.slack_api("chat.postEphemeral", token, json={"channel": channel_id, "user": slack_user, "text": text})
    publish_admin_center(org, admin)


# Which tab a modal belongs to, for the permission check on submit.
_SUBMIT_TAB = {
    "aidl_add_admin": "add-admin",
    "aidl_add_user": "add-user",
    "aidl_card_request": "cards",
    "aidl_card_send": "cards",
    "aidl_card_new": "cards",
    "aidl_add_app": "ai-apps",
}


def _handle_submission(payload: dict):
    view = payload.get("view") or {}
    callback = view.get("callback_id", "")
    tab = _SUBMIT_TAB.get(callback)
    if tab is None:
        return HttpResponse(status=200)
    admin = _admin_for(payload)
    org = get_organization_for_user(admin) if admin else None
    if admin is None or admin.role != AIDLUser.Role.ADMIN or org is None:
        return JsonResponse(slack_modals.errors(**{_first_block(view): "Only AIDL admins can do this."}))
    d = build_admin_cards_context(admin)
    allowed = tab in d["tabs"]
    if callback in ("aidl_card_request", "aidl_card_send"):
        allowed = d["can_manage_cards"]
    elif callback == "aidl_card_new":
        allowed = d["can_create_card"]
    if not allowed:
        return JsonResponse(slack_modals.errors(**{_first_block(view): "Your admin permissions don't include this."}))

    team_id = org.slack_team_id
    handlers = {
        "aidl_add_admin": lambda: slack_modals.submit_add_admin(admin, org, team_id, view),
        "aidl_add_user": lambda: slack_modals.submit_add_user(admin, org, team_id, view),
        "aidl_card_request": lambda: slack_modals.submit_card_request(admin, org, view),
        "aidl_card_send": lambda: slack_modals.submit_card_send(admin, org, view),
        "aidl_card_new": lambda: slack_modals.submit_new_card(admin, org, view),
        "aidl_add_app": lambda: slack_modals.submit_add_app(admin, org, view),
    }
    result = handlers[callback]() or {}
    if "response_action" in result:
        return JsonResponse(result)
    meta = json.loads(view.get("private_metadata") or "{}")
    notice = result.get("notice", "")
    slack_modals.in_background(_notify, org, meta.get("channel_id", ""), payload["user"]["id"], notice, admin)
    # Close every stacked modal (e.g. Send Cards list → card detail).
    return JsonResponse({"response_action": "clear"})


def _send_coverage(org, slack_user: str, view_id: str, download_url: str) -> None:
    """Also DM the CSV file. The popup's Download CSV button works either
    way; if the DM copy fails, the popup says why."""
    result = slack_modals.send_coverage_csv(org, slack_user)
    if result.get("ok") or not view_id:
        return
    if result.get("error") == "missing_scope":
        note = ("📎 *aidl-coverage.csv* — the DM copy needs the *files:write* bot scope in the Slack app "
                "(then sign in with Slack again). The Download CSV button works without it.")
    else:
        note = f"📎 *aidl-coverage.csv* — couldn't send the DM copy ({result.get('error')})."
    view = slack_modals.coverage_view(org, download_url=download_url, note=note)
    slack_client.slack_api("views.update", slack_client.bot_token(org), json={"view_id": view_id, "view": view})


def _first_block(view: dict) -> str:
    for block in view.get("blocks") or []:
        if block.get("type") == "input":
            return block["block_id"]
    return "name"


@csrf_exempt
@require_POST
def slack_interactions(request):
    if not verify_slack_signature(request):
        return HttpResponse("invalid signature", status=401)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
    except ValueError:
        return HttpResponse(status=400)
    if payload.get("type") == "view_submission":
        return _handle_submission(payload)
    if payload.get("type") != "block_actions":
        return HttpResponse(status=200)

    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id", "")
    if action_id.startswith("aidl_link_"):
        return HttpResponse(status=200)  # plain link button — Slack opened the URL

    tab = action.get("value") or "home"
    response_url = payload.get("response_url", "")
    is_ephemeral = bool((payload.get("container") or {}).get("is_ephemeral"))

    user = _admin_for(payload)
    if user is None:
        _reply(
            response_url,
            text=f"Sign in to AIDL with Slack first: {settings.FRONTEND_URL.rstrip('/')}/home",
        )
        return HttpResponse(status=200)
    if user.role != AIDLUser.Role.ADMIN:
        _reply(response_url, text="The AIDL Admin Center is only available to AIDL admins.")
        return HttpResponse(status=200)

    d = build_admin_cards_context(user)
    org = get_organization_for_user(user)

    # Buttons inside an open modal: view one card / request a new card.
    if action_id in ("aidl_card_view", "aidl_card_new") and org is not None:
        meta = json.loads((payload.get("view") or {}).get("private_metadata") or "{}")
        if action_id == "aidl_card_view":
            card = next((c for c in d["cards"] if c["id"] == action.get("value")), None)
            if card is not None:
                _open_modal(org, payload.get("trigger_id", ""), slack_modals.card_view(d, card, meta), push=True)
        elif d["can_create_card"]:
            _open_modal(org, payload.get("trigger_id", ""), slack_modals.new_card_view(meta), push=True)
        return HttpResponse(status=200)

    if tab not in d["tabs"]:
        _reply(response_url, text="Your admin permissions don't include that tab.")
        return HttpResponse(status=200)

    # Export Coverage CSV: report in a modal + the CSV file by DM (guide 8.1).
    if action_id == "aidl_open_home" and org is not None:
        download_url = _private_link(request, user, "csv")
        view_id = _open_modal(org, payload.get("trigger_id", ""), slack_modals.coverage_view(org, download_url=download_url))
        slack_modals.in_background(_send_coverage, org, (payload.get("user") or {}).get("id", ""), view_id, download_url)
        return HttpResponse(status=200)

    # Card form buttons ("Send Admin Invite", "Issue License", ...) open a modal.
    if action_id.startswith("aidl_open_") and tab != "home" and org is not None:
        meta = {"channel_id": (payload.get("channel") or {}).get("id") or org.slack_channel_id}
        view = slack_modals.view_for_tab(tab, d, meta)
        if view is not None:
            _open_modal(org, payload.get("trigger_id", ""), view)
        return HttpResponse(status=200)

    blocks = admin_card_blocks(d, tab, open_url=_private_link(request, user, tab))
    _reply(response_url, text=admin_card_text(d), blocks=blocks, replace=is_ephemeral)
    return HttpResponse(status=200)
