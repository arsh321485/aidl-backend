"""Microsoft Teams / OAuth auth APIs."""

import json

from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .auth_jwt import create_access_token, create_refresh_token, decode_token
from .microsoft_auth import (
    consume_oauth_state_with_payload,
    build_auth_url,
    build_teams_launch_url,
    consume_oauth_state,
    ensure_aidl_channel,
    exchange_code_for_token,
    fetch_microsoft_profile,
    microsoft_configured,
    resolve_teams_url,
)
from .slack_auth import build_slack_auth_url, exchange_slack_code, fetch_slack_profile, slack_configured
from . import slack_client
from .org_policy import PolicyAnswersError, clean_answers, policy_completed, save_answers
from .teams_cards import org_display_name
from .teams_channel_tabs import is_aidl_teams_landing_url
from .org_service import ensure_organization_for_login, get_organization_for_user, replace_welcome_card
from .models import AIDLUser, Invitation, Organization
from .schema import AuthResponseDoc, ErrorDoc, MeDoc, MessageDoc, RefreshRequestDoc, TokenPairDoc
from .serializers import AIDLUserSerializer, LoginSerializer, SignupSerializer


class JWTAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header.split(" ", 1)[1].strip()
        try:
            payload = decode_token(token)
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationFailed("Invalid or expired token") from exc
        if payload.get("type") != "access":
            raise AuthenticationFailed("Access token required")
        try:
            user = AIDLUser.objects.get(pk=payload["sub"], is_active=True)
        except AIDLUser.DoesNotExist as exc:
            raise AuthenticationFailed("User not found") from exc
        return (user, token)


def _normalize_enroll_as(value: str) -> str:
    value = (value or "individual").strip().lower()
    if value not in {"individual", "organization"}:
        return "individual"
    return value


def _issue_tokens(user: AIDLUser) -> dict:
    return {
        "access_token": create_access_token(user),
        "refresh_token": create_refresh_token(user),
        "token_type": "Bearer",
        "user": AIDLUserSerializer(user).data,
    }


def _upsert_user(profile: dict, enroll_as: str, refresh_token: str = "") -> tuple[AIDLUser, bool]:
    defaults = {
        "email": profile.get("email", ""),
        "full_name": profile.get("full_name", ""),
        "enroll_as": enroll_as,
        "provider": "teams",
        "last_login_at": timezone.now(),
        "is_active": True,
    }
    # Only overwrite when we actually got one this login (offline_access scope) —
    # a token exchange without it must not wipe out a previously stored one.
    if refresh_token:
        defaults["ms_refresh_token"] = refresh_token
    user, created = AIDLUser.objects.update_or_create(
        microsoft_id=profile["microsoft_id"],
        defaults=defaults,
    )
    return user, created


def _save_channel_on_user(user: AIDLUser, channel_info: dict | None) -> None:
    if not channel_info:
        user.teams_team_id = ""
        user.teams_channel_id = ""
        user.teams_channel_name = ""
    else:
        user.teams_team_id = channel_info.get("team_id") or ""
        user.teams_channel_id = channel_info.get("channel_id") or ""
        user.teams_channel_name = channel_info.get("channel_name") or ""
    user.save(
        update_fields=[
            "teams_team_id",
            "teams_channel_id",
            "teams_channel_name",
            "updated_at",
        ]
    )


def _accept_pending_invitation(user: AIDLUser) -> None:
    """Best-effort: mark a matching Invitation accepted once the invited
    person actually signs in. Org/role placement already happened via Team
    membership + ensure_organization_for_login — this is bookkeeping only,
    so it must never block login if it fails."""
    if not user.email:
        return
    try:
        Invitation.objects.filter(
            email__iexact=user.email, status=Invitation.Status.PENDING
        ).update(status=Invitation.Status.ACCEPTED, accepted_at=timezone.now())
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("accept invitation failed for %s: %s", user.email, exc)


def _redirect_with_tokens(
    user: AIDLUser,
    mode: str = "microsoft",
    ms_access_token: str = "",
    teams_url: str = "",
    teams_channel_url: str = "",
    teams_setup: str = "",
    welcome_card_sent: bool = False,
    welcome_card_error: str = "",
    channel_name: str = "",
    channel_tabs_created: int = 0,
    channel_tabs_ok: bool = False,
):
    tokens = _issue_tokens(user)
    # Chat/home fallback for this Microsoft account.
    platform_url = build_teams_launch_url(user.email)
    # AIDL channel deep link (when MS_AIDL_TEAM_ID + Graph succeed) — this is what should
    # actually open after login, not chat.
    channel_url = (teams_channel_url or "").strip()
    if not channel_url and teams_url and "/l/channel/" in teams_url:
        channel_url = teams_url
    if not channel_url:
        channel_url = resolve_teams_url(
            email=user.email,
            ms_access_token=ms_access_token,
            team_id=getattr(user, "teams_team_id", "") or "",
            channel_id=getattr(user, "teams_channel_id", "") or "",
            channel_name=getattr(user, "teams_channel_name", "") or "",
        )
        if channel_url and not is_aidl_teams_landing_url(channel_url):
            channel_url = ""

    query = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "enroll_as": user.enroll_as,
        "mode": mode,
        "email": user.email,
        "full_name": user.full_name,
        # Primary: open THIS in a new tab/window → AIDL channel when available,
        # else falls back to Teams chat/home.
        "teams_url": channel_url or platform_url,
        # Secondary: Teams chat/home, kept for a "switch to chat" link on the frontend.
        "teams_platform_url": platform_url,
        "open_teams": "1",
        "teams_connected": "true",
        # posts = Adaptive Card in channel; home_tab = website tab; channel = posts fallback; chat = Teams home
        "landed_on": (
            "posts"
            if channel_url and "/l/channel/" in channel_url
            else (
                "home_tab"
                if channel_url and "/l/entity/" in channel_url
                else ("channel" if channel_url else "chat")
            )
        ),
    }
    if teams_setup:
        query["teams_setup"] = teams_setup
    if welcome_card_sent:
        query["welcome_card_sent"] = "1"
    elif welcome_card_error:
        query["welcome_card_sent"] = "0"
        query["welcome_card_error"] = welcome_card_error[:160]
    if channel_url:
        query["teams_channel_url"] = channel_url
        query["teams_home_tab_url"] = channel_url
    if channel_name:
        query["teams_channel_name"] = channel_name
    if channel_tabs_created:
        query["channel_tabs_created"] = str(channel_tabs_created)
    if channel_tabs_ok:
        query["channel_tabs_ok"] = "1"
    if ms_access_token:
        query["ms_access_token"] = ms_access_token
    return HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")


@extend_schema(
    summary="Redirect straight to Microsoft sign-in",
    parameters=[OpenApiParameter("enroll_as", str, enum=["individual", "organization"], default="individual")],
    responses={302: OpenApiResponse(description="Redirect to Microsoft login"), 503: ErrorDoc},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def teams_login_redirect(request):
    """
    Plain HTTP redirect straight into Microsoft sign-in — for links that must
    work from outside the SPA (e.g. an invite email), where nothing can call
    teams_login's JSON auth_url and redirect the browser itself.
    """
    enroll_as = _normalize_enroll_as(request.query_params.get("enroll_as"))
    if not microsoft_configured():
        return Response(
            {"error": "microsoft_not_configured"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    data = build_auth_url(enroll_as)
    return HttpResponseRedirect(data["auth_url"])


@extend_schema(
    summary="Start Microsoft / Teams login (returns auth_url)",
    parameters=[OpenApiParameter("enroll_as", str, enum=["individual", "organization"], default="individual")],
    responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def teams_login(request):
    """
    Start Teams/Microsoft signup/login.
    Query: enroll_as=individual|organization
    Returns auth_url for Microsoft login.
    """
    enroll_as = _normalize_enroll_as(request.query_params.get("enroll_as"))

    if not microsoft_configured():
        return Response(
            {
                "error": "microsoft_not_configured",
                "message": "Set MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID, and MS_REDIRECT_URI.",
                "required_env": [
                    "MS_CLIENT_ID",
                    "MS_CLIENT_SECRET",
                    "MS_TENANT_ID",
                    "MS_REDIRECT_URI",
                ],
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    data = build_auth_url(enroll_as)
    data["mode"] = "microsoft"
    data["after_login"] = {
        "open_teams": True,
        "note": (
            "After callback, ALWAYS window.open(teams_url) when open_teams=1. "
            "Backend auto-creates/finds Microsoft Team 'AIDL' and channel "
            "'aidl dashboard', installs Home/Learner's Permit/Highway Code/"
            "Traffic Light Check tabs, then sets teams_url to the Home tab deep link "
            "(landed_on=channel). Falls back to Teams chat/home (landed_on=chat) "
            "if Graph/Team.Create fails. teams_platform_url is always chat/home."
        ),
        "aidl_channel": {
            "team_id_configured": bool((settings.MS_AIDL_TEAM_ID or "").strip()),
            "team_name": settings.MS_AIDL_TEAM_NAME or "AIDL",
            "channel_name": settings.MS_AIDL_CHANNEL_NAME or "aidl dashboard",
            "auto_create_team": bool(getattr(settings, "MS_AIDL_AUTO_CREATE_TEAM", True)),
        },
    }
    return Response(data)


@extend_schema(
    summary="Microsoft OAuth callback — do not call from the frontend",
    responses={302: OpenApiResponse(description="Redirect to AUTH_SUCCESS_REDIRECT with tokens")},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def teams_callback(request):
    """Microsoft redirects here with ?code=&state=."""
    error = request.query_params.get("error")
    if error:
        return Response(
            {
                "error": error,
                "error_description": request.query_params.get("error_description"),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code:
        return Response(
            {"error": "code is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    enroll_as, state_error = consume_oauth_state(state)
    if not enroll_as:
        enroll_as = _normalize_enroll_as(
            request.query_params.get("enroll_as") or "organization"
        )

    if not microsoft_configured():
        return Response(
            {"error": "microsoft_not_configured"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    token_result = exchange_code_for_token(code)
    if "access_token" not in token_result:
        return Response(
            {
                "error": "token_exchange_failed",
                "details": token_result.get("error_description")
                or token_result.get("error"),
                "state_warning": state_error,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    ms_token = token_result["access_token"]
    profile = fetch_microsoft_profile(ms_token)
    if not profile.get("microsoft_id"):
        return Response(
            {"error": "unable_to_fetch_profile"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user, is_new_signup = _upsert_user(profile, enroll_as, token_result.get("refresh_token") or "")
    old_team_id = getattr(user, "teams_team_id", "") or ""
    old_channel_id = getattr(user, "teams_channel_id", "") or ""

    channel_info = ensure_aidl_channel(ms_token, email=user.email)
    _save_channel_on_user(user, channel_info)
    # Refresh user after channel ids saved
    user.refresh_from_db()
    try:
        ensure_organization_for_login(
            user,
            channel_info=channel_info or {},
            org_name=org_display_name(),
        )
        user.refresh_from_db()
    except Exception as exc:  # noqa: BLE001
        # Login must not break if org bootstrap fails.
        import logging

        logging.getLogger(__name__).warning("ensure organization failed: %s", exc)

    _accept_pending_invitation(user)

    channel_url = (channel_info or {}).get("teams_url") or ""
    posts_url = (channel_info or {}).get("channel_posts_url") or channel_url
    home_tab_url = (channel_info or {}).get("home_tab_url") or ""
    # Prefer Posts deep link so Adaptive Card is visible in-channel.
    landing_url = posts_url or channel_url or home_tab_url

    is_learner = user.role == AIDLUser.Role.LEARNER
    if is_learner:
        # Learners get their own 4-tab dashboard (Home / Learner's Permit /
        # Highway Code / Traffic Light Check) instead of the shared Admin
        # Center card — the channel's Posts stream is shared by everyone in
        # the org, so only an admin's login should replace what's posted
        # there. teams_tab_page routes "home" to this dashboard instead of
        # Admin Center's Home once it sees this user's role is Learner.
        from urllib.parse import urlencode

        teams_base = (getattr(settings, "MS_TEAMS_APP_BASE_URL", "") or "").rstrip("/")
        landing_url = f"{teams_base}/tabs/home/?" + urlencode(
            {"email": user.email, "full_name": user.full_name}
        )

    welcome_card_sent = False
    welcome_card_error = ""
    channel_tabs_created = 0
    if channel_info and not is_learner:
        # Phase 1: post Admin Center card on EVERY successful channel ensure
        # (not only first signup) so Posts is never empty after login.
        send_every_login = getattr(settings, "MS_SEND_ADMIN_CARD_EVERY_LOGIN", True)
        channel_recreated = (
            (channel_info.get("team_id") or "") != old_team_id
            or (channel_info.get("channel_id") or "") != old_channel_id
        )
        should_send_welcome = bool(send_every_login) or is_new_signup or channel_recreated
        if should_send_welcome:
            welcome_result = replace_welcome_card(
                ms_token,
                team_id=channel_info.get("team_id") or "",
                channel_id=channel_info.get("channel_id") or "",
                full_name=user.full_name,
                org_name=user.organization_name or org_display_name(),
                email=user.email,
                user=user,
                channel_just_created=bool(
                    channel_info.get("channel_just_created")
                ),
            )
            welcome_card_sent = bool(welcome_result and welcome_result.get("ok"))
            if not welcome_card_sent:
                welcome_card_error = (
                    (welcome_result or {}).get("error")
                    or (welcome_result or {}).get("detail")
                    or "send_failed"
                )[:180]
        tab_info = channel_info.get("channel_tabs") or {}
        channel_tabs_created = len(tab_info.get("tabs_created") or [])

    return _redirect_with_tokens(
        user,
        mode="microsoft",
        ms_access_token=ms_token,
        teams_channel_url=landing_url,
        teams_setup="ok" if channel_info else "failed",
        welcome_card_sent=welcome_card_sent,
        welcome_card_error=welcome_card_error,
        channel_name=(channel_info or {}).get("channel_name") or "",
        channel_tabs_created=channel_tabs_created,
        channel_tabs_ok=bool(
            ((channel_info or {}).get("channel_tabs") or {}).get("ok")
        ),
    )


@extend_schema(summary="Teams launch URL for the signed-in user", responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_launch(request):
    """Return Microsoft Teams platform URL (+ optional AIDL channel deep link)."""
    platform_url = build_teams_launch_url(getattr(request.user, "email", ""))
    channel_url = resolve_teams_url(
        email=getattr(request.user, "email", ""),
        team_id=getattr(request.user, "teams_team_id", "") or "",
        channel_id=getattr(request.user, "teams_channel_id", "") or "",
        channel_name=getattr(request.user, "teams_channel_name", "") or "",
    )
    if channel_url and not is_aidl_teams_landing_url(channel_url):
        channel_url = ""
    return Response(
        {
            "teams_connected": True,
            "email": request.user.email,
            "full_name": request.user.full_name,
            # Primary: AIDL Home tab deep link when available, else chat/home.
            "teams_url": channel_url or platform_url,
            "teams_platform_url": platform_url,
            "teams_channel_url": channel_url or None,
            "landed_on": (
                "home_tab"
                if channel_url and "/l/entity/" in channel_url
                else ("channel" if channel_url else "chat")
            ),
            "channel": {
                "team_id": getattr(request.user, "teams_team_id", "") or "",
                "channel_id": getattr(request.user, "teams_channel_id", "") or "",
                "channel_name": getattr(request.user, "teams_channel_name", "")
                or (settings.MS_AIDL_CHANNEL_NAME or "aidl dashboard"),
            },
            "message": (
                "Open teams_url — it opens AIDL → aidl dashboard → Home tab when ready. "
                "Do NOT open teams_platform_url for primary landing (that is Chat)."
            ),
        }
    )


def _slack_error_redirect(error: str, description: str = ""):
    query = {"mode": "slack", "error": error}
    if description:
        query["error_description"] = description
    return HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")


def _upsert_slack_user(profile: dict, enroll_as: str) -> AIDLUser:
    """Find the user by Slack id, else by verified email (links an existing
    website/Teams account), else create one. Slack users reuse the unique
    microsoft_id field as `slack:<team_id>:<user_id>`."""
    user = AIDLUser.objects.filter(microsoft_id=profile["slack_id"]).first()
    if not user and profile["email"] and profile["email_verified"]:
        user = AIDLUser.objects.filter(email__iexact=profile["email"]).first()

    if user:
        if not user.is_active:
            return None
        user.last_login_at = timezone.now()
        if not user.avatar_url and profile["avatar_url"]:
            user.avatar_url = profile["avatar_url"]
        user.save(update_fields=["last_login_at", "avatar_url", "updated_at"])
        return user

    is_org = enroll_as == AIDLUser.EnrollAs.ORGANIZATION
    return AIDLUser.objects.create(
        microsoft_id=profile["slack_id"],
        provider="slack",
        email=profile["email"],
        first_name=profile["first_name"],
        last_name=profile["last_name"],
        full_name=profile["full_name"]
        or f"{profile['first_name']} {profile['last_name']}".strip(),
        avatar_url=profile["avatar_url"],
        enroll_as=enroll_as,
        role=AIDLUser.Role.ADMIN if is_org else AIDLUser.Role.LEARNER,
        organization_name=profile["team_name"] if is_org else "",
        last_login_at=timezone.now(),
        is_active=True,
    )


def _new_slack_organization(name: str, team_id: str) -> Organization:
    base = _slack_slug(name)
    slug, n = base, 1
    while Organization.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return Organization.objects.create(
        name=name,
        slug=slug,
        slack_team_id=team_id,
        seats_purchased=int(getattr(settings, "AIDL_DEFAULT_SEATS", 50) or 50),
        admin_seat_limit=int(getattr(settings, "AIDL_DEFAULT_ADMIN_SEATS", 3) or 3),
    )


def _slack_slug(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "org"


def _ensure_slack_organization(user: AIDLUser, profile: dict) -> Organization:
    """Guide section 6: an organization login creates the AIDL organization,
    or reuses it when this Slack workspace is already connected.

    Any company can install AIDL, so the Slack workspace id alone decides
    the organization — never the workspace name (two companies can share a
    name) and never another workspace's organization."""
    team_id = profile.get("team_id") or ""
    team_name = profile.get("team_name") or user.organization_name or "My organization"
    org = Organization.objects.filter(slack_team_id=team_id, is_active=True).first() if team_id else None
    if org is None:
        current = get_organization_for_user(user) if user.organization_id else None
        if current is not None and not current.slack_team_id:
            # Same company signed up on the website / Teams first → connect
            # its Slack workspace to that organization.
            org = current
            org.slack_team_id = team_id
            org.save(update_fields=["slack_team_id", "updated_at"])
        else:
            org = _new_slack_organization(team_name, team_id)
    user.organization_id = str(org.pk)
    user.organization_name = org.name
    user.save(update_fields=["organization_id", "organization_name", "updated_at"])
    # Links the user, seeds the default app registry, and makes them admin
    # (organization enrollment, or the first member of the organization).
    return ensure_organization_for_login(user, org_name=org.name)


def _setup_slack_workspace(org: Organization, user: AIDLUser, install: dict, profile: dict) -> str:
    """Guide section 7.1: store the bot install, create/reuse #aidl, add the
    admin, post the Admin Center card. Returns the channel link ("" if the
    channel could not be created — the admin is still logged in)."""
    from .slack_blocks import publish_admin_center

    slack_client.save_install(org, install)
    if not slack_client.ensure_channel(org, profile.get("slack_user_id", "")):
        return ""
    publish_admin_center(org, user)
    return slack_client.channel_url(org)


@extend_schema(
    summary="Start Sign in with Slack (returns auth_url)",
    parameters=[OpenApiParameter("enroll_as", str, enum=["individual", "organization"], default="individual")],
    responses={200: OpenApiTypes.OBJECT, 503: ErrorDoc},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def slack_login(request):
    """Query: enroll_as=individual|organization. Returns auth_url for Slack login."""
    enroll_as = _normalize_enroll_as(request.query_params.get("enroll_as"))
    # Guide 4.7: the admin isn't logged in while answering the policy
    # questions, so the answers ride along in the OAuth state.
    payload = ""
    raw_policy = request.query_params.get("policy")
    if raw_policy:
        try:
            payload = json.dumps({"policy_answers": clean_answers(json.loads(raw_policy))})
        except (ValueError, PolicyAnswersError):
            return Response({"policy": "Answer all 8 policy questions first."}, status=status.HTTP_400_BAD_REQUEST)
    if not slack_configured():
        return Response(
            {
                "error": "slack_not_configured",
                "message": "Set SLACK_CLIENT_ID, SLACK_CLIENT_SECRET and SLACK_REDIRECT_URI.",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    # Organizations install the AIDL app ("Add to Slack") so the backend can
    # create #aidl and post the Admin cards; individuals just sign in.
    if enroll_as == AIDLUser.EnrollAs.ORGANIZATION:
        data = slack_client.build_install_url(enroll_as, payload)
    else:
        data = build_slack_auth_url(enroll_as)
    data["mode"] = "slack"
    return Response(data)


@extend_schema(
    summary="Slack OAuth callback — do not call from the frontend",
    responses={302: OpenApiResponse(description="Redirect to AUTH_SUCCESS_REDIRECT with tokens or error")},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def slack_callback(request):
    """Slack redirects here with ?code=&state= (or ?error=)."""
    error = request.query_params.get("error")
    if error:
        return _slack_error_redirect(error, "Slack sign-in was cancelled or denied.")

    code = request.query_params.get("code")
    if not code:
        return _slack_error_redirect("code_missing", "Slack did not return an authorization code.")

    enroll_as, state_payload, state_error = consume_oauth_state_with_payload(request.query_params.get("state"))
    if state_error == "state_not_found" or state_error == "state_expired":
        return _slack_error_redirect(state_error, "Sign-in session expired. Please try again.")
    enroll_as = _normalize_enroll_as(enroll_as or "individual")
    try:
        policy_answers = json.loads(state_payload).get("policy_answers") if state_payload else None
    except ValueError:
        policy_answers = None

    if not slack_configured():
        return _slack_error_redirect("slack_not_configured")

    is_org = enroll_as == AIDLUser.EnrollAs.ORGANIZATION
    token_result = slack_client.exchange_install_code(code) if is_org else exchange_slack_code(code)
    if not token_result.get("ok") or not token_result.get("access_token"):
        return _slack_error_redirect(
            "token_exchange_failed", str(token_result.get("error") or "Slack token exchange failed.")
        )

    if is_org:
        profile = slack_client.installer_profile(token_result)
    else:
        profile = fetch_slack_profile(token_result["access_token"])
    if not profile.get("slack_id"):
        return _slack_error_redirect("unable_to_fetch_profile", "Could not read your Slack profile.")

    user = _upsert_slack_user(profile, enroll_as)
    if not user:
        return _slack_error_redirect("account_disabled", "This AIDL account is disabled.")
    slack_url = ""
    if is_org:
        try:
            org = _ensure_slack_organization(user, profile)
            user.refresh_from_db()
            if policy_answers:
                save_answers(org, policy_answers)
            slack_url = _setup_slack_workspace(org, user, token_result, profile)
        except Exception as exc:  # noqa: BLE001
            # Login must not break if org / channel setup fails (same as Teams).
            import logging

            logging.getLogger(__name__).warning("slack workspace setup failed: %s", exc)
    _accept_pending_invitation(user)

    tokens = _issue_tokens(user)
    query = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "mode": "slack",
        "enroll_as": user.enroll_as,
        "email": user.email,
        "full_name": user.full_name,
    }
    if is_org:
        # Guide 7.1: open Slack on #aidl; landed_on=chat when the channel
        # couldn't be created (the frontend then shows a hint instead).
        team_id = profile.get("team_id", "")
        query["slack_url"] = slack_url or f"https://app.slack.com/client/{team_id}"
        query["landed_on"] = "channel" if slack_url else "chat"
        query["open_slack"] = "1"
        # Guide 4.7: tells the frontend whether the org still has to answer.
        query["policy_completed"] = "1" if policy_completed(get_organization_for_user(user)) else "0"
    return HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")


@extend_schema(summary="Current user profile", responses=MeDoc)
@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def me(request):
    data = AIDLUserSerializer(request.user).data
    platform_url = build_teams_launch_url(request.user.email)
    channel_url = resolve_teams_url(
        email=request.user.email,
        team_id=getattr(request.user, "teams_team_id", "") or "",
        channel_id=getattr(request.user, "teams_channel_id", "") or "",
        channel_name=getattr(request.user, "teams_channel_name", "") or "",
    )
    channel_url = channel_url if channel_url and is_aidl_teams_landing_url(channel_url) else None
    data["teams_url"] = channel_url or platform_url
    data["teams_platform_url"] = platform_url
    data["teams_channel_url"] = channel_url
    data["landed_on"] = (
        "home_tab"
        if channel_url and "/l/entity/" in channel_url
        else ("channel" if channel_url else "chat")
    )
    data["teams_connected"] = True
    return Response(data)


@extend_schema(
    summary="Website signup (Individual / Organization)",
    request=SignupSerializer,
    responses={
        201: AuthResponseDoc,
        400: OpenApiResponse(OpenApiTypes.OBJECT, description="Field errors, e.g. {\"email\": [\"...\"]}"),
    },
)
@api_view(["POST"])
@permission_classes([AllowAny])
def signup(request):
    """
    Website registration (Individual / Organization).
    Body matches the public signup form. Returns JWT tokens + user.
    """
    serializer = SignupSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()

    if user.enroll_as == AIDLUser.EnrollAs.ORGANIZATION:
        try:
            ensure_organization_for_login(user, org_name=user.organization_name)
            user.refresh_from_db()
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning(
                "website signup org bootstrap failed: %s", exc
            )

    user.last_login_at = timezone.now()
    user.save(update_fields=["last_login_at", "updated_at"])
    payload = _issue_tokens(user)
    payload["message"] = "Account created successfully."
    return Response(payload, status=status.HTTP_201_CREATED)


@extend_schema(
    summary="Website sign-in (also served at /api/auth/login/)",
    request=LoginSerializer,
    responses={
        200: AuthResponseDoc,
        400: OpenApiResponse(OpenApiTypes.OBJECT, description="Field errors, e.g. {\"password\": [\"Incorrect password.\"]}"),
    },
)
@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    """
    Website sign-in (Individual / Organization).
    Body: enroll_as, email, password. Returns JWT tokens + user.
    """
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.validated_data["user"]
    user.last_login_at = timezone.now()
    user.save(update_fields=["last_login_at", "updated_at"])
    payload = _issue_tokens(user)
    payload["message"] = "Signed in successfully."
    return Response(payload)


signin = login


@extend_schema(
    summary="Exchange a refresh token for a new token pair",
    request=RefreshRequestDoc,
    responses={200: TokenPairDoc, 400: ErrorDoc, 401: ErrorDoc},
)
@api_view(["POST"])
@permission_classes([AllowAny])
def refresh(request):
    refresh_token = request.data.get("refresh_token")
    if not refresh_token:
        return Response(
            {"error": "refresh_token is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        payload = decode_token(refresh_token)
    except Exception:  # noqa: BLE001
        return Response({"error": "invalid refresh token"}, status=status.HTTP_401_UNAUTHORIZED)

    if payload.get("type") != "refresh":
        return Response({"error": "refresh token required"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        user = AIDLUser.objects.get(pk=payload["sub"], is_active=True)
    except AIDLUser.DoesNotExist:
        return Response({"error": "user not found"}, status=status.HTTP_401_UNAUTHORIZED)

    return Response(
        {
            "access_token": create_access_token(user),
            "refresh_token": create_refresh_token(user),
            "token_type": "Bearer",
        }
    )


@extend_schema(summary="Logout (client deletes its tokens)", request=None, responses=MessageDoc)
@api_view(["POST"])
@permission_classes([AllowAny])
def logout(request):
    return Response({"message": "Logged out. Delete tokens on client."})
