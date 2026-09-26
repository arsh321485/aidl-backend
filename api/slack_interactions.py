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
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .auth_jwt import create_access_token
from .models import AIDLUser
from .slack_blocks import admin_card_blocks, admin_card_text
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
    if tab == "home":
        return f"{base}/api/slack/cards/admin/coverage.csv?token={token}"
    return f"{base}/api/slack/cards/admin/?token={token}#{tab}"


@csrf_exempt
@require_POST
def slack_interactions(request):
    if not verify_slack_signature(request):
        return HttpResponse("invalid signature", status=401)
    try:
        payload = json.loads(request.POST.get("payload", "{}"))
    except ValueError:
        return HttpResponse(status=400)
    if payload.get("type") != "block_actions":
        return HttpResponse(status=200)

    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id", "")
    if action_id.startswith("aidl_link_"):
        return HttpResponse(status=200)  # plain link button — Slack opened the URL

    tab = action.get("value") or "home"
    response_url = payload.get("response_url", "")
    is_ephemeral = bool((payload.get("container") or {}).get("is_ephemeral"))
    team_id = (payload.get("team") or {}).get("id", "")
    slack_user = (payload.get("user") or {}).get("id", "")

    user = AIDLUser.objects.filter(microsoft_id=f"slack:{team_id}:{slack_user}", is_active=True).first()
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
    if tab not in d["tabs"]:
        _reply(response_url, text="Your admin permissions don't include that tab.")
        return HttpResponse(status=200)

    blocks = admin_card_blocks(d, tab, open_url=_private_link(request, user, tab))
    _reply(response_url, text=admin_card_text(d), blocks=blocks, replace=is_ephemeral)
    return HttpResponse(status=200)
