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
    build_teams_launch_url,
    consume_oauth_state,
    exchange_code_for_token,
    fetch_microsoft_profile,
    microsoft_configured,
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


def _redirect_with_tokens(user: AIDLUser, mode: str = "microsoft", ms_access_token: str = ""):
    tokens = _issue_tokens(user)
    teams_url = build_teams_launch_url(user.email)
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
        "note": "After callback, open returned teams_url (includes login_hint for same email).",
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

    profile = fetch_microsoft_profile(token_result["access_token"])
    if not profile.get("microsoft_id"):
        return Response(
            {"error": "unable_to_fetch_profile"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = _upsert_user(profile, enroll_as)
    return _redirect_with_tokens(
        user,
        mode="microsoft",
        ms_access_token=token_result.get("access_token", ""),
    )


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_launch(request):
    """Return Microsoft Teams web URL for the signed-in AIDL user."""
    teams_url = build_teams_launch_url(getattr(request.user, "email", ""))
    return Response(
        {
            "teams_connected": True,
            "email": request.user.email,
            "full_name": request.user.full_name,
            "teams_url": teams_url,
            "message": "Open teams_url to launch Microsoft Teams for this account.",
        }
    )


@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def me(request):
    data = AIDLUserSerializer(request.user).data
    data["teams_url"] = build_teams_launch_url(request.user.email)
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
