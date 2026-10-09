"""Auto-onboarding: anyone who joins the organization's AIDL channel gets an
AIDL account and their learner cards — no "Add User" step.

Slack app → Event Subscriptions → Request URL <backend>/api/slack/events/,
bot events `member_joined_channel` and `member_left_channel`.
"""

from __future__ import annotations

import json
import logging

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import slack_client
from .models import AIDLUser, Organization
from .plans import can_join, usage
from .slack_interactions import verify_slack_signature
from .slack_modals import _upsert_member, _warn_if_dm_failed, in_background

logger = logging.getLogger(__name__)


def primary_admin(org: Organization) -> AIDLUser | None:
    """The organization's first admin: its recorded owner, else the oldest
    active admin."""
    admins = AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True)
    if org.owner_user_id:
        owner = admins.filter(pk=org.owner_user_id).first()
        if owner is not None:
            return owner
    return admins.order_by("created_at").first()


def _existing_member(org: Organization, slack_user_id: str, email: str) -> AIDLUser | None:
    user = AIDLUser.objects.filter(microsoft_id=f"slack:{org.slack_team_id}:{slack_user_id}").first()
    if user is None and email:
        user = AIDLUser.objects.filter(email__iexact=email, organization_id=str(org.pk)).first()
    return user


def _dm_admin(token: str, org: Organization, text: str) -> None:
    admin = primary_admin(org)
    if admin is not None and admin.microsoft_id.startswith("slack:"):
        slack_client.send_dm(token, org, admin.microsoft_id.split(":")[-1], text=text,
                             blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])


def _refuse_join(token: str, org: Organization, slack_user_id: str, name: str) -> None:
    """The plan is full: take the person out of the channel again and tell
    both sides why. They get no AIDL account and no licence."""
    u = usage(org)
    slack_client.slack_api("conversations.kick", token, json={"channel": org.slack_channel_id, "user": slack_user_id})
    slack_client.send_dm(token, org, slack_user_id, text="AIDL: this team is full", blocks=[
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"👋 Hi {name.split()[0] if name else 'there'} — {org.name}'s AIDL *{u['label']}* plan already has "
            f"all {u['limit']} users, so you couldn't be added to the AIDL channel yet. "
            "Your AIDL admin has been told."}}])
    _dm_admin(token, org,
              f"⛔ *{name or 'Someone'}* was added to the AIDL channel, but your *{u['label']}* plan is full "
              f"({u['used']} of {u['limit']} users), so AIDL removed them again.\n"
              "To add them: remove someone who hasn't earned a license yet, or upgrade to the *Basic* plan.")


def onboard_member(org: Organization, slack_user_id: str, *, force: bool = False) -> str:
    """Create the member's AIDL account and DM their Welcome + learner
    dashboard. Returns what happened: 'onboarded', 'rejoined', 'already',
    'admin', 'bot', 'other_org', 'plan_full' or 'error'."""
    from .package_delivery import sync_package
    from .slack_blocks import user_dashboard_blocks, user_welcome_blocks

    token = slack_client.bot_token(org)
    if not token or slack_user_id in ("", org.slack_bot_user_id, "USLACKBOT"):
        return "bot"
    info = slack_client.slack_api("users.info", token, params={"user": slack_user_id})
    if not info.get("ok"):
        return "error"
    user = info.get("user") or {}
    if user.get("is_bot") or user.get("deleted") or user.get("is_app_user"):
        return "bot"
    profile = user.get("profile") or {}
    existing = _existing_member(org, slack_user_id, (profile.get("email") or "").strip())
    if not can_join(org, existing):
        _refuse_join(token, org, slack_user_id, profile.get("real_name") or profile.get("display_name") or "")
        return "plan_full"
    member = _upsert_member(org, org.slack_team_id, slack_user_id, profile)
    if member is None:
        return "other_org"
    if member.role == AIDLUser.Role.ADMIN:
        return "admin"  # admins already get the Admin Center

    if member.slack_left_at:  # added back after being removed
        member.slack_left_at = None
        member.save(update_fields=["slack_left_at", "updated_at"])
        if member.slack_onboarded_at and not force:
            tab = "license" if member.licence_issued else "home"
            slack_client.send_dm(token, org, slack_user_id, text="Welcome back to AIDL",
                                 blocks=user_dashboard_blocks(member, org, tab))
            sync_package(org, member)  # resumes the cards they hadn't had yet
            return "rejoined"
    if member.slack_onboarded_at and not force:
        return "already"
    # Claim the welcome atomically: "Add people" and the channel-join event can
    # both arrive for the same person.
    claimed = AIDLUser.objects.filter(pk=member.pk, slack_onboarded_at__isnull=True).update(
        slack_onboarded_at=timezone.now())
    if not claimed and not force:
        return "already"
    member.refresh_from_db()

    from .admin_setup import first_tab

    first = (member.full_name or "there").split()[0]
    tab = first_tab(member, org)  # the first learning card the admin has sent
    results = [
        slack_client.send_dm(token, org, slack_user_id, text=f"Welcome to AIDL, {first}!",
                             blocks=user_welcome_blocks(member, org)),
        slack_client.send_dm(token, org, slack_user_id, text="Your AIDL dashboard",
                             blocks=user_dashboard_blocks(member, org, tab)),
    ]
    admin = primary_admin(org)
    if admin is not None:
        _warn_if_dm_failed(token, org, admin, member, results)
    return "onboarded"


def offboard_member(org: Organization, slack_user_id: str) -> str:
    """Someone left / was removed from the AIDL channel: they leave the team,
    their licence is suspended and their package cards stop. A licence they
    earned keeps counting toward the plan (no seat recycling); the seat of
    someone without a licence is freed. Returns 'removed', 'admin' or
    'unknown'."""
    from .package_delivery import pause_package

    member = AIDLUser.objects.filter(microsoft_id=f"slack:{org.slack_team_id}:{slack_user_id}",
                                     organization_id=str(org.pk)).first()
    if member is None:
        return "unknown"  # e.g. refused because the plan was full
    if member.role == AIDLUser.Role.ADMIN:
        return "admin"
    if member.slack_left_at:
        return "removed"
    member.slack_left_at = timezone.now()
    member.save(update_fields=["slack_left_at", "updated_at"])
    cancelled = pause_package(org, member)
    token = slack_client.bot_token(org)
    u = usage(org)
    if member.licence_issued:
        detail = (f"Their license `{member.licence_number}` is suspended"
                  + (f" and {cancelled} upcoming card(s) were cancelled" if cancelled else "")
                  + f". The license still counts toward your plan ({u['used']} of {u['limit']} users), "
                  "and adding them back restores it.")
    else:
        detail = f"They hadn't earned a license, so their seat is free again ({u['used']} of {u['limit']} users)."
    _dm_admin(token, org, f"🚪 *{member.full_name or member.email}* left the AIDL channel. {detail}")
    return "removed"


def onboard_channel_members(org: Organization) -> dict:
    """One-off: onboard people who were already in the channel before
    auto-onboarding existed (they never trigger a 'joined' event)."""
    token = slack_client.bot_token(org)
    counts: dict[str, int] = {}
    cursor = ""
    while True:
        data = slack_client.slack_api("conversations.members", token,
                                      params={"channel": org.slack_channel_id, "limit": 200, "cursor": cursor})
        for uid in data.get("members") or []:
            result = onboard_member(org, uid)
            counts[result] = counts.get(result, 0) + 1
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor or not data.get("ok"):
            break
    return counts


@csrf_exempt
@require_POST
def slack_events(request):
    """Slack Events API: URL verification + member_joined_channel /
    member_left_channel."""
    if not verify_slack_signature(request):
        return HttpResponse("invalid signature", status=401)
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    if payload.get("type") == "url_verification":
        return JsonResponse({"challenge": payload.get("challenge", "")})
    if request.headers.get("X-Slack-Retry-Num"):
        return HttpResponse(status=200)  # already handled the first delivery

    event = payload.get("event") or {}
    kind = event.get("type")
    if payload.get("type") == "event_callback" and kind in ("member_joined_channel", "member_left_channel"):
        team_id = payload.get("team_id") or event.get("team", "")
        org = Organization.objects.filter(slack_team_id=team_id, is_active=True).first()
        if org is not None and event.get("channel") == org.slack_channel_id:
            handler = onboard_member if kind == "member_joined_channel" else offboard_member
            in_background(handler, org, event.get("user", ""))
    return HttpResponse(status=200)
