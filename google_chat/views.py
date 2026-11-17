"""Google sign-in. Individual accounts get a session. Organizations also get an AIDL space."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api.auth_jwt import create_access_token, create_refresh_token
from api.microsoft_auth import consume_oauth_state_with_payload
from api.models import AIDLUser
from api.org_policy import PolicyAnswersError, clean_answers, policy_completed, save_answers
from api.org_service import get_organization_for_user

from . import accounts
from .auth import build_login_url, configured, exchange_code, fetch_profile

logger = logging.getLogger(__name__)


def _enroll_as(value: str) -> str:
    value = (value or "individual").strip().lower()
    if value not in {"individual", "organization"}:
        return "individual"
    return value


def _error(error: str, description: str = ""):
    query = {"mode": "google_chat", "error": error}
    if description:
        query["error_description"] = description
    return HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")


@extend_schema(
    summary="Start Sign in with Google Chat (returns auth_url)",
    parameters=[OpenApiParameter("enroll_as", str, enum=["individual", "organization"], default="individual")],
    responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def google_login(request):
    """Query: enroll_as=individual|organization. Organization login also installs Chat."""
    enroll_as = _enroll_as(request.query_params.get("enroll_as"))
    payload = ""
    raw_policy = request.query_params.get("policy")
    if raw_policy:
        try:
            payload = json.dumps({"policy_answers": clean_answers(json.loads(raw_policy))})
        except (ValueError, PolicyAnswersError):
            return Response({"policy": "Answer all 8 policy questions first."}, status=400)
    if not configured():
        return Response(
            {
                "error": "google_chat_not_configured",
                "message": "Set GOOGLE_CHAT_CLIENT_ID, GOOGLE_CHAT_CLIENT_SECRET and GOOGLE_CHAT_REDIRECT_URI.",
            },
            status=503,
        )
    data = build_login_url(enroll_as, payload)
    data["mode"] = "google_chat"
    return Response(data)


@extend_schema(
    summary="Google OAuth callback — the browser is redirected here",
    responses={302: OpenApiResponse(description="Redirect to AUTH_SUCCESS_REDIRECT with tokens or error")},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def google_callback(request):
    error = request.query_params.get("error")
    if error:
        return _error(error, "Google sign-in was cancelled or denied.")
    code = request.query_params.get("code")
    if not code:
        return _error("code_missing", "Google did not return an authorization code.")

    enroll_as, state_payload, state_error = consume_oauth_state_with_payload(request.query_params.get("state"))
    if state_error in ("state_not_found", "state_expired", "missing_state"):
        return _error(state_error or "missing_state", "Sign-in session expired. Please try again.")
    enroll_as = _enroll_as(enroll_as or "individual")
    try:
        policy_answers = json.loads(state_payload).get("policy_answers") if state_payload else None
    except ValueError:
        policy_answers = None
    if not configured():
        return _error("google_chat_not_configured")

    token = exchange_code(code)
    if not token.get("ok"):
        return _error("token_exchange_failed", str(token.get("error") or "Google token exchange failed."))
    profile = fetch_profile(token["access_token"])
    if not profile.get("google_id"):
        return _error("unable_to_fetch_profile", "Could not read your Google profile.")

    is_org = enroll_as == AIDLUser.EnrollAs.ORGANIZATION
    if is_org and not profile.get("hosted_domain"):
        return _error("workspace_required", "Use a Google Workspace account to set up an organization.")
    if is_org:
        refusal = accounts.already_set_up(profile)
        if refusal:
            return _error("already_set_up", refusal)

    user = accounts.upsert_user(profile, enroll_as)
    if user is None:
        return _error("account_disabled", "This AIDL account is disabled.")

    space_url = ""
    if is_org:
        try:
            org = accounts.ensure_organization(user, profile)
            user.refresh_from_db()
            if policy_answers:
                save_answers(org, policy_answers)
            space_url = accounts.setup_workspace(org, user, token, profile)
        except Exception as exc:  # noqa: BLE001
            logger.warning("google chat workspace setup failed: %s", exc)
    accounts.accept_pending_invitation(user)

    query = {
        "access_token": create_access_token(user),
        "refresh_token": create_refresh_token(user),
        "mode": "google_chat",
        "enroll_as": user.enroll_as,
        "email": user.email,
        "full_name": user.full_name,
    }
    if is_org:
        query["google_chat_url"] = space_url
        query["landed_on"] = "space" if space_url else "chat"
        query["open_google_chat"] = "1"
        query["policy_completed"] = "1" if policy_completed(get_organization_for_user(user)) else "0"
    return HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")
