"""Microsoft Teams / OAuth auth APIs."""

from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .auth_jwt import create_access_token, create_refresh_token, decode_token
from .microsoft_auth import (
    build_auth_url,
    consume_oauth_state,
    ensure_aidl_channel,
    exchange_code_for_token,
    fetch_microsoft_profile,
    microsoft_configured,
    resolve_teams_url,
)
from .models import AIDLUser
from .serializers import AIDLUserSerializer


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


def _upsert_user(profile: dict, enroll_as: str) -> AIDLUser:
    user, _created = AIDLUser.objects.update_or_create(
        microsoft_id=profile["microsoft_id"],
        defaults={
            "email": profile.get("email", ""),
            "full_name": profile.get("full_name", ""),
            "enroll_as": enroll_as,
            "provider": "teams",
            "last_login_at": timezone.now(),
            "is_active": True,
        },
    )
    return user


def _save_channel_on_user(user: AIDLUser, channel_info: dict | None) -> None:
    if not channel_info:
        return
    user.teams_team_id = channel_info.get("team_id") or user.teams_team_id
    user.teams_channel_id = channel_info.get("channel_id") or user.teams_channel_id
    user.teams_channel_name = channel_info.get("channel_name") or user.teams_channel_name
    user.save(
        update_fields=[
            "teams_team_id",
            "teams_channel_id",
            "teams_channel_name",
            "updated_at",
        ]
    )


def _redirect_with_tokens(
    user: AIDLUser,
    mode: str = "microsoft",
    ms_access_token: str = "",
    teams_url: str = "",
):
    tokens = _issue_tokens(user)
    if not teams_url:
        teams_url = resolve_teams_url(
            email=user.email,
            ms_access_token=ms_access_token,
            team_id=getattr(user, "teams_team_id", "") or "",
            channel_id=getattr(user, "teams_channel_id", "") or "",
            channel_name=getattr(user, "teams_channel_name", "") or "",
        )
    query = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "enroll_as": user.enroll_as,
        "mode": mode,
        "email": user.email,
        "full_name": user.full_name,
        "teams_url": teams_url,
        "open_teams": "1",
        "teams_connected": "true",
    }
    if ms_access_token:
        query["ms_access_token"] = ms_access_token
    return HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")


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
        "teams_url": "https://teams.microsoft.com/",
        "note": (
            "After callback, open returned teams_url. "
            "When MS_AIDL_TEAM_ID is set, teams_url is an AIDL channel deep link."
        ),
        "aidl_channel": {
            "team_id_configured": bool((settings.MS_AIDL_TEAM_ID or "").strip()),
            "channel_name": settings.MS_AIDL_CHANNEL_NAME or "AIDL",
        },
    }
    return Response(data)


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

    user = _upsert_user(profile, enroll_as)

    channel_info = ensure_aidl_channel(ms_token, email=user.email)
    _save_channel_on_user(user, channel_info)
    teams_url = (channel_info or {}).get("teams_url") or resolve_teams_url(
        email=user.email,
        team_id=user.teams_team_id,
        channel_id=user.teams_channel_id,
        channel_name=user.teams_channel_name,
    )

    return _redirect_with_tokens(
        user,
        mode="microsoft",
        ms_access_token=ms_token,
        teams_url=teams_url,
    )


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_launch(request):
    """Return Microsoft Teams URL for the signed-in user (AIDL channel deep link when available)."""
    teams_url = resolve_teams_url(
        email=getattr(request.user, "email", ""),
        team_id=getattr(request.user, "teams_team_id", "") or "",
        channel_id=getattr(request.user, "teams_channel_id", "") or "",
        channel_name=getattr(request.user, "teams_channel_name", "") or "",
    )
    return Response(
        {
            "teams_connected": True,
            "email": request.user.email,
            "full_name": request.user.full_name,
            "teams_url": teams_url,
            "channel": {
                "team_id": getattr(request.user, "teams_team_id", "") or "",
                "channel_id": getattr(request.user, "teams_channel_id", "") or "",
                "channel_name": getattr(request.user, "teams_channel_name", "")
                or (settings.MS_AIDL_CHANNEL_NAME or "AIDL"),
            },
            "message": "Open teams_url to launch Microsoft Teams (AIDL channel when configured).",
        }
    )


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def me(request):
    data = AIDLUserSerializer(request.user).data
    data["teams_url"] = resolve_teams_url(
        email=request.user.email,
        team_id=getattr(request.user, "teams_team_id", "") or "",
        channel_id=getattr(request.user, "teams_channel_id", "") or "",
        channel_name=getattr(request.user, "teams_channel_name", "") or "",
    )
    data["teams_connected"] = True
    return Response(data)


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


@api_view(["POST"])
@permission_classes([AllowAny])
def logout(request):
    return Response({"message": "Logged out. Delete tokens on client."})
