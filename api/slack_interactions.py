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
from . import admin_setup, slack_client, slack_modals
from .org_service import get_organization_for_user
from .slack_blocks import admin_card_blocks, admin_card_text, highway_code_full_view, publish_admin_center, rating_blocks
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
    from .slack_blocks import has_images, text_fallback

    try:
        resp = requests.post(response_url, json=body, timeout=10)
        failed = resp.status_code != 200 or '"ok":false' in resp.text.replace(" ", "")
        if failed and body.get("blocks") and has_images(body["blocks"]):
            # Slack couldn't load an image (e.g. the backend isn't reachable) —
            # send the same card as plain text.
            resp = requests.post(response_url, json={**body, "blocks": text_fallback(body["blocks"])}, timeout=10)
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


# Slack drops a popup opened more than 3 seconds after the click, and building
# the Admin Center data can take longer. Popup buttons therefore open a
# "Loading…" popup at once (_preopen); _open_modal then fills that popup in.
_PREOPENED: dict[str, str] = {}
_PREOPEN_LOCK = threading.Lock()
_POPUP_TITLES = {
    "aidl_policy_open": "AI policy questions", "aidl_aup_view": "Team AUP", "aidl_aup_delegate": "Ask someone else",
    "aidl_add_people": "Add your team", "aidl_team_progress": "Team progress", "aidl_card_view": "Card",
    "aidl_card_new": "Request a new card", "aidl_open_home": "Coverage", "aidl_open_add-admin": "Add admin",
    "aidl_open_cards": "Send Cards", "aidl_open_ai-apps": "Add AI application", "aidl_open_it-apps": "Add IT application",
}


def _preopen(org, trigger_id: str, title: str, push: bool) -> None:
    method = "views.push" if push else "views.open"
    view = {"type": "modal", "callback_id": "aidl_loading", "title": {"type": "plain_text", "text": title[:24]},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "⏳ Loading…"}}]}
    result = slack_client.slack_api(method, slack_client.bot_token(org), json={"trigger_id": trigger_id, "view": view})
    view_id = (result.get("view") or {}).get("id", "")
    if view_id:
        with _PREOPEN_LOCK:
            _PREOPENED[trigger_id] = view_id


def _finish_preopen(org, trigger_id: str) -> None:
    """The click didn't open a popup after all (e.g. no permission)."""
    with _PREOPEN_LOCK:
        view_id = _PREOPENED.pop(trigger_id, "")
    if view_id:
        slack_client.slack_api("views.update", slack_client.bot_token(org), json={"view_id": view_id, "view": {
            "type": "modal", "title": {"type": "plain_text", "text": "AIDL"}, "close": {"type": "plain_text", "text": "Close"},
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                                                    "text": "Your admin permissions don't include this."}}]}})


def _open_modal(org, trigger_id: str, view: dict, push: bool = False) -> str:
    """Opens (or pushes) a modal; returns its view id. If a "Loading…" popup
    was already opened for this click, it is filled in instead."""
    with _PREOPEN_LOCK:
        preopened = _PREOPENED.pop(trigger_id, "") if trigger_id else ""
    if preopened:
        result = slack_client.slack_api("views.update", slack_client.bot_token(org),
                                        json={"view_id": preopened, "view": view})
        if not result.get("ok"):
            logger.warning("slack views.update failed: %s %s", result.get("error"), result.get("response_metadata"))
        return (result.get("view") or {}).get("id", "") or preopened
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
    "aidl_card_request": "cards",
    "aidl_card_send": "cards",
    "aidl_card_new": "cards",
    "aidl_add_app": "ai-apps",
    "aidl_policy": "home",
    "aidl_delegate_aup": "home",
    "aidl_add_people": "home",
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
        "aidl_card_request": lambda: slack_modals.submit_card_request(admin, org, view),
        "aidl_card_send": lambda: slack_modals.submit_card_send(admin, org, view),
        "aidl_card_new": lambda: slack_modals.submit_new_card(admin, org, view),
        "aidl_add_app": lambda: slack_modals.submit_add_app(admin, org, view),
        "aidl_policy": lambda: slack_modals.submit_policy(admin, org, view),
        "aidl_delegate_aup": lambda: slack_modals.submit_delegate(admin, org, team_id, view),
        "aidl_add_people": lambda: slack_modals.submit_add_people(admin, org, view),
    }
    result = handlers[callback]() or {}
    if "response_action" in result:
        return JsonResponse(result)
    meta = json.loads(view.get("private_metadata") or "{}")
    notice = result.get("notice", "")
    slack_modals.in_background(_notify, org, meta.get("channel_id", ""), payload["user"]["id"], notice, admin)
    if "view" in result:  # show a result page instead of closing (policy answers report)
        return JsonResponse({"response_action": "update", "view": result["view"]})
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
        reason = slack_client.DM_BLOCKED.get(result.get("error"), result.get("error"))
        note = f"📎 *aidl-coverage.csv* — couldn't send the DM copy ({reason}). The Download CSV button works."
    view = slack_modals.coverage_view(org, download_url=download_url, note=note)
    slack_client.slack_api("views.update", slack_client.bot_token(org), json={"view_id": view_id, "view": view})


def _open_coverage(org, trigger_id: str, slack_user: str, download_url: str) -> None:
    view_id = _open_modal(org, trigger_id, slack_modals.coverage_view(org, download_url=download_url))
    _send_coverage(org, slack_user, view_id, download_url)


def _switch_user_tab(org, member, payload: dict, tab: str) -> None:
    from .slack_blocks import user_dashboard_blocks

    container = payload.get("container") or {}
    channel = container.get("channel_id") or (payload.get("channel") or {}).get("id", "")
    ts = container.get("message_ts") or (payload.get("message") or {}).get("ts", "")
    if not (channel and ts):
        return
    slack_client.slack_api("chat.update", slack_client.bot_token(org), json={
        "channel": channel, "ts": ts, "text": "AIDL dashboard", "blocks": user_dashboard_blocks(member, org, tab)})


def _send_aup_reminders(org, admin, response_url: str) -> None:
    """Home card 'Send reminders': DM every member who hasn't signed the AUP."""
    token = slack_client.bot_token(org)
    members = AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True, aup_signed=False,
                                      microsoft_id__startswith=f"slack:{org.slack_team_id}:").exclude(
                                      role=AIDLUser.Role.ADMIN)
    sent = 0
    for member in members:
        result = slack_client.send_dm(token, org, member.microsoft_id.split(":")[-1],
                                      text="Reminder: please sign your organization's AI Acceptable Use Policy",
                                      blocks=[
                                          _ctx_line(org.name),
                                          {"type": "section", "text": {"type": "mrkdwn", "text": (
                                              f"📄 *Reminder from {admin.full_name or 'your AIDL admin'}*\n"
                                              f"Please read and sign {org.name}'s AI Acceptable Use Policy. "
                                              "Until you do, the ethics gate blocks your license upgrade.")}},
                                      ])
        sent += 1 if result.get("ok") or result.get("fallback_ok") else 0
    _reply(response_url, text=f"✓ AUP reminder sent to {sent} of {members.count()} unsigned user(s).")


def _send_aup_to_team(org, response_url: str) -> None:
    """Admin 'Send AUP to team' (older buttons): same as setup step 4."""
    sent, total = admin_setup.send_item(org, "aup")
    _reply(response_url, text=f"✓ AUP sent to {sent} of {total} team member(s).")


def _send_or_remind(org, admin, item: str, *, remind: bool, response_url: str, view_id: str) -> None:
    """Setup step 4: send a learning card to the team (once — later joiners
    get it automatically) or remind the people who haven't accepted it."""
    if remind:
        sent, total = admin_setup.remind_item(org, item)
        text = f"🔔 Reminder sent to {sent} of {total} who haven't accepted {admin_setup.ITEM_LABEL[item]}."
    else:
        sent, total = admin_setup.send_item(org, item)
        text = (f"✓ {admin_setup.ITEM_LABEL[item]} sent to {sent} of {total} team member(s). "
                "People who join later get it automatically.")
    if view_id:  # clicked inside the AUP-ready popup
        slack_client.slack_api("views.update", slack_client.bot_token(org), json={
            "view_id": view_id, "view": slack_modals.policy_summary_view(org, notice=text)})
    elif response_url:
        _reply(response_url, text=text)
    publish_admin_center(org, admin)  # the step moves on


def _ctx_line(org_name: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*AIDL* · APP  for {org_name}"}]}


_ACK_TAB = {"highway_code": "highway-code", "traffic_light": "traffic-light", "aup": "aup"}


def _acknowledge(org, member, payload: dict, item: str) -> None:
    """Record the acknowledgement; once both are done the Learner licence is
    issued automatically and the message flips to the License tab."""
    from .licensing import acknowledge, issue_learner_if_ready
    from .slack_blocks import user_dashboard_blocks

    if item not in _ACK_TAB:
        return
    if item == "aup":
        from .aup import generate_aup

        acknowledge(member, item, generate_aup(org)["version"])
    else:
        acknowledge(member, item)
    result = issue_learner_if_ready(member, org)
    member.refresh_from_db()
    tab = "license" if result in ("issued", "no_seats") else _ACK_TAB[item]
    container = payload.get("container") or {}
    channel = container.get("channel_id") or (payload.get("channel") or {}).get("id", "")
    ts = container.get("message_ts") or (payload.get("message") or {}).get("ts", "")
    token = slack_client.bot_token(org)
    if channel and ts:
        slack_client.slack_api("chat.update", token, json={
            "channel": channel, "ts": ts, "text": "AIDL dashboard", "blocks": user_dashboard_blocks(member, org, tab)})
    slack_user = (payload.get("user") or {}).get("id", "")
    if result == "issued":
        slack_client.send_dm(token, org, slack_user, text="🎉 Your Learner's Permit is ready!", blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text":
                f"🎉 *Your Learner's Permit is ready!*  `{member.licence_number}`\nOpen the *License* tab above to download or share it."}}])
        from .package_delivery import sync_package

        sync_package(org, member)  # card 1 now, the rest of the plan's package scheduled
    elif result == "no_seats":
        from .plans import user_limit
        from .slack_onboarding import primary_admin

        admin = primary_admin(org)
        if admin is not None:
            slack_client.send_dm(token, org, admin.microsoft_id.split(":")[-1],
                                 text="AIDL: no free license seats", blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    f"⏳ *{member.full_name or member.email}* finished the Highway Code and Traffic Light Check, "
                    f"but all {user_limit(org)} licenses on your plan are used. Their license is waiting for a free seat."}}])


def _remove_person(org, admin, action_id: str, user_id: str, view_id: str, meta: str) -> None:
    """Remove an admin / team member, then redraw the popup with the result."""
    from .admin_people import remove_admin, remove_member

    if action_id == "aidl_admin_remove":
        done, message = remove_admin(org, admin, user_id)
        d = build_admin_cards_context(admin)
        d["notice"] = message if done else f"⚠️ {message}"
        view = slack_modals.add_admin_view(d, json.loads(meta))
    else:
        done, message = remove_member(org, admin, user_id)
        view = slack_modals.team_progress_view(org, notice=message if done else f"⚠️ {message}")
    if view_id:
        slack_client.slack_api("views.update", slack_client.bot_token(org), json={"view_id": view_id, "view": view})
    if done:
        publish_admin_center(org, admin)  # numbers on Home changed


def _org_for_team(payload: dict):
    from .models import Organization

    team_id = (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id", "")
    return Organization.objects.filter(slack_team_id=team_id, is_active=True).first() if team_id else None


def _rate_traffic_light(org, payload: dict, response_url: str) -> None:
    """Guide 10.4 — 👍/👎: count +1, 'Thanks for the feedback', one vote per
    person. The card's counts are updated for everyone in the conversation."""
    from django.db import IntegrityError

    from .models import TrafficLightRating, TrafficLightVote

    vote = "like" if payload["actions"][0]["action_id"] == "aidl_rate_like" else "dislike"
    message = payload.get("message") or {}
    message_ts = message.get("ts") or (payload.get("container") or {}).get("message_ts", "")
    slack_user = (payload.get("user") or {}).get("id", "")
    try:
        TrafficLightVote.objects.create(organization_id=str(org.pk), message_ts=message_ts,
                                        slack_user_id=slack_user, vote=vote)
    except IntegrityError:
        _reply(response_url, text="You've already rated this card — thanks!")
        return

    rating = TrafficLightRating.objects.first() or TrafficLightRating.objects.create()
    if vote == "like":
        rating.likes += 1
    else:
        rating.dislikes += 1
    rating.save()

    blocks = message.get("blocks") or []
    for block in blocks:
        if block.get("block_id") == "aidl_rate":
            block["elements"] = rating_blocks(rating.likes, rating.dislikes)[1]["elements"]
    channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id", "")
    if blocks and channel and message_ts:
        slack_client.slack_api("chat.update", slack_client.bot_token(org), json={
            "channel": channel, "ts": message_ts, "blocks": blocks, "text": message.get("text", "Traffic Light Check")})
    _reply(response_url, text="✓ Thanks for the feedback!" if vote == "like" else "✓ Feedback noted, thank you")


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

    # "I've read and accept" on the Highway Code / Traffic Light Check.
    if action_id.startswith("aidl_ack_"):
        org = _org_for_team(payload)
        member = _admin_for(payload)
        if org is not None and member is not None:
            slack_modals.in_background(_acknowledge, org, member, payload, action.get("value", ""))
        return HttpResponse(status=200)

    # User dashboard tabs (guide 10): swap the card shown in that message.
    if action_id.startswith("aidl_utab_"):
        org = _org_for_team(payload)
        member = _admin_for(payload)  # any AIDL account for this Slack user (learner or admin)
        if org is not None and member is not None:
            slack_modals.in_background(_switch_user_tab, org, member, payload, action.get("value") or "license")
        return HttpResponse(status=200)

    # User cards (guide 10.3 / 10.4) — for everyone in the workspace, not only admins.
    if action_id in ("aidl_hc_full", "aidl_rate_like", "aidl_rate_dislike"):
        org = _org_for_team(payload)
        if org is not None:
            if action_id == "aidl_hc_full":
                slack_modals.in_background(_open_modal, org, payload.get("trigger_id", ""),
                                           highway_code_full_view(org))
            else:
                slack_modals.in_background(_rate_traffic_light, org, payload, response_url)
        return HttpResponse(status=200)

    # Admin buttons: answer Slack at once (it allows 3 seconds, and building
    # the Admin Center from the database can take longer), then do the work.
    slack_modals.in_background(_admin_action, _BaseUrl(request.build_absolute_uri("/")), payload, action,
                               action_id, tab, response_url, is_ephemeral)
    return HttpResponse(status=200)


class _BaseUrl:
    """Stands in for the request in the background: _private_link only needs
    the site's base URL."""

    def __init__(self, base: str):
        self.base = base

    def build_absolute_uri(self, path: str = "/") -> str:
        return self.base.rstrip("/") + path


def _admin_action(request, payload: dict, action: dict, action_id: str, tab: str,
                  response_url: str, is_ephemeral: bool) -> None:
    """Admin Center button clicks (run after Slack has had its 200)."""
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

    trigger_id = payload.get("trigger_id", "")
    org = get_organization_for_user(user)
    if org is not None and trigger_id and action_id in _POPUP_TITLES:
        _preopen(org, trigger_id, _POPUP_TITLES[action_id], push=bool((payload.get("view") or {}).get("id")))
    try:
        _admin_action_body(request, payload, action, action_id, tab, response_url, is_ephemeral, user, org)
    finally:
        if org is not None and trigger_id:
            _finish_preopen(org, trigger_id)


def _admin_action_body(request, payload: dict, action: dict, action_id: str, tab: str,
                       response_url: str, is_ephemeral: bool, user, org) -> None:
    d = build_admin_cards_context(user)

    # Buttons inside an open modal: view one card / request a new card.
    if action_id in ("aidl_card_view", "aidl_card_new") and org is not None:
        meta = json.loads((payload.get("view") or {}).get("private_metadata") or "{}")
        if action_id == "aidl_card_view":
            card = next((c for c in d["cards"] if c["id"] == action.get("value")), None)
            if card is not None:
                _open_modal(org, payload.get("trigger_id", ""),
                                           slack_modals.card_view(d, card, meta), push=True)
        elif d["can_create_card"]:
            _open_modal(org, payload.get("trigger_id", ""),
                                       slack_modals.new_card_view(meta), push=True)
        return HttpResponse(status=200)

    # Remove buttons in the Add Admin and Team progress popups.
    if action_id in ("aidl_admin_remove", "aidl_member_remove") and org is not None:
        view = payload.get("view") or {}
        slack_modals.in_background(_remove_person, org, user, action_id, action.get("value", ""),
                                   view.get("id", ""), view.get("private_metadata") or "{}")
        return HttpResponse(status=200)

    # The 8 policy questions (asked in Slack, not on the website). Until they're
    # answered, the AUP buttons open them too.
    policy_set = d["policy"].get("has_answers")
    if org is not None and d["can_policy"] and (action_id == "aidl_policy_open"
                            or (action_id in ("aidl_aup_view", "aidl_aup_send") and not policy_set)):
        meta = {"channel_id": (payload.get("channel") or {}).get("id") or org.slack_channel_id}
        _open_modal(org, payload.get("trigger_id", ""), slack_modals.policy_view(org, meta))
        return HttpResponse(status=200)

    in_modal = bool((payload.get("view") or {}).get("id"))
    if action_id == "aidl_aup_view":
        if org is not None:  # from the AUP-ready popup it opens on top of it
            _open_modal(org, payload.get("trigger_id", ""), slack_modals.aup_view(org),
                                       push=in_modal)
        return HttpResponse(status=200)

    # Setup steps (admin_setup.py).
    if org is not None and action_id in ("aidl_aup_send", "aidl_send_item", "aidl_remind_item"):
        item = "aup" if action_id == "aidl_aup_send" else action.get("value", "")
        if item in admin_setup.ITEMS and d["can_send"]:
            _send_or_remind(org, user, item, remind=action_id == "aidl_remind_item",
                            response_url=response_url, view_id=(payload.get("view") or {}).get("id", ""))
        return HttpResponse(status=200)

    if org is not None and action_id == "aidl_aup_delegate" and d["can_admins"]:
        meta = {"channel_id": (payload.get("channel") or {}).get("id") or org.slack_channel_id}
        _open_modal(org, payload.get("trigger_id", ""), slack_modals.delegate_view(meta))
        return HttpResponse(status=200)

    if org is not None and action_id == "aidl_add_people" and d["can_team"]:
        meta = {"channel_id": (payload.get("channel") or {}).get("id") or org.slack_channel_id}
        _open_modal(org, payload.get("trigger_id", ""), slack_modals.add_people_view(meta, d))
        return HttpResponse(status=200)

    if org is not None and action_id == "aidl_setup_skip" and d["can_admins"]:
        admin_setup.update(org, **{f"{action.get('value', 'admins')}_step": "skipped"})
        publish_admin_center(org, user)
        if is_ephemeral:
            _reply(response_url, text=admin_card_text(d),
                   blocks=admin_card_blocks(build_admin_cards_context(user), "home"), replace=True)
        return HttpResponse(status=200)

    if action_id == "aidl_team_progress":
        if org is not None:
            _open_modal(org, payload.get("trigger_id", ""),
                                       slack_modals.team_progress_view(org))
        return HttpResponse(status=200)

    if action_id == "aidl_aup_remind":
        if org is not None:
            slack_modals.in_background(_send_aup_reminders, org, user, response_url)
        return HttpResponse(status=200)

    if tab not in d["tabs"]:
        _reply(response_url, text="Your admin permissions don't include that tab.")
        return HttpResponse(status=200)

    # Export Coverage CSV: report in a modal + the CSV file by DM (guide 8.1).
    if action_id == "aidl_open_home" and org is not None:
        download_url = _private_link(request, user, "csv")
        _open_coverage(org, payload.get("trigger_id", ""),
                                   (payload.get("user") or {}).get("id", ""), download_url)
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
