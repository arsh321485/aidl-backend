"""GET / POST /api/org/policy-answers/ — the organization's 8 policy answers
(AIDL Slack guide 4.7). Saved against the organization, not the user."""

import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .auth_views import JWTAuthentication
from .models import AIDLUser
from .org_policy import POLICY_QUESTIONS, PolicyAnswersError, get_answers, save_answers
from .org_service import get_organization_for_user

logger = logging.getLogger(__name__)


@extend_schema(
    summary="Read or save the organization's 8 policy answers",
    request=OpenApiTypes.OBJECT,
    responses=OpenApiTypes.OBJECT,
)
@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def policy_answers(request):
    org = get_organization_for_user(request.user)
    if org is None:
        return Response({"detail": "Your account is not linked to an organization."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "POST":
        if request.user.role != AIDLUser.Role.ADMIN:
            return Response({"detail": "Only organization admins can change the policy answers."}, status=status.HTTP_403_FORBIDDEN)
        try:
            save_answers(org, request.data.get("answers"))
        except PolicyAnswersError as exc:
            return Response(exc.errors, status=status.HTTP_400_BAD_REQUEST)
        # The Admin cards' data depends on the answers (guide 7.3) — refresh
        # the Home card in Slack straight away.
        if org.slack_channel_id:
            try:
                from .slack_blocks import publish_admin_center

                publish_admin_center(org, request.user)
            except Exception as exc:  # noqa: BLE001
                logger.warning("refreshing the Slack Admin Center failed: %s", exc)

    answers = get_answers(org)
    return Response(
        {
            "answers": answers,
            "policy_completed": bool(answers),
            "questions": {qid: list(options) for qid, options in POLICY_QUESTIONS.items()},
        }
    )
