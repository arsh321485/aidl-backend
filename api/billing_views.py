"""Plans page API (billing.py). Bearer token = the admin's AIDL access JWT
from the Slack "Upgrade plan" link.

    GET  /api/billing/plans/      current plan, the plans, topics, schedule
    POST /api/billing/preview/    {plan, topics} → {cards: first 12 it adds (titles only), total}
    POST /api/billing/checkout/   {plan, topics, schedule?, seats?} →
                                  {status: redirect|activated|pending, url?, message}
"""

from __future__ import annotations

import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import billing
from .auth_views import JWTAuthentication
from .models import AIDLUser
from .org_service import get_organization_for_user

logger = logging.getLogger(__name__)


def _org_for_admin(request):
    if request.user.role != AIDLUser.Role.ADMIN:
        return None, Response({"detail": "Only AIDL admins can manage the plan."}, status=status.HTTP_403_FORBIDDEN)
    org = get_organization_for_user(request.user)
    if org is None:
        return None, Response({"detail": "Your account is not linked to an organization."},
                              status=status.HTTP_404_NOT_FOUND)
    return org, None


def _topics(value) -> list[str]:
    if isinstance(value, str):
        value = [v for v in value.split(",") if v]
    return [str(v).strip() for v in (value or []) if str(v).strip()]


@extend_schema(summary="Plans page: current plan, plans, topics", responses=OpenApiResponse(OpenApiTypes.OBJECT))
@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def plans_view(request):
    org, error = _org_for_admin(request)
    if error:
        return error
    from .admin_perms import has

    data = billing.catalog(org)
    data["can_upgrade"] = has(org, request.user, "billing")
    return Response(data)


@extend_schema(summary="Preview the cards a plan adds for the chosen topics", request=OpenApiTypes.OBJECT,
               responses=OpenApiResponse(OpenApiTypes.OBJECT))
@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def preview_view(request):
    org, error = _org_for_admin(request)
    if error:
        return error
    return Response(billing.preview(org, str(request.data.get("plan") or ""), _topics(request.data.get("topics"))))


@extend_schema(summary="Upgrade: go to payment (or activate in test mode)", request=OpenApiTypes.OBJECT,
               responses={200: OpenApiResponse(OpenApiTypes.OBJECT), 400: OpenApiResponse(OpenApiTypes.OBJECT)})
@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def checkout_view(request):
    org, error = _org_for_admin(request)
    if error:
        return error
    from .admin_perms import has

    if not has(org, request.user, "billing"):
        return Response({"detail": "Only the master admin, or admins allowed to manage the plan, can upgrade."},
                        status=status.HTTP_403_FORBIDDEN)
    seats = request.data.get("seats")
    try:
        seats = int(seats) if seats not in (None, "") else None
    except (TypeError, ValueError):
        return Response({"seats": ["Enter a number."]}, status=status.HTTP_400_BAD_REQUEST)
    schedule = request.data.get("schedule") if isinstance(request.data.get("schedule"), dict) else None
    try:
        result = billing.checkout(org, request.user, str(request.data.get("plan") or ""),
                                  _topics(request.data.get("topics")), schedule, seats)
    except billing.UpgradeError as exc:
        return Response({k: [v] for k, v in exc.errors.items()}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001 — e.g. Stripe refused the session
        from .stripe_billing import StripeError

        if not isinstance(exc, StripeError):
            raise
        return Response({"detail": f"Payment could not be started: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(result)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe → AIDL: activate a paid upgrade / downgrade a cancelled one."""
    from . import stripe_billing

    try:
        event = stripe_billing.verify(request.body, request.headers.get("Stripe-Signature", ""))
    except (stripe_billing.StripeError, ValueError) as exc:
        logger.warning("stripe webhook rejected: %s", exc)
        return HttpResponse(status=400)
    logger.info("stripe webhook %s: %s", event.get("type"), stripe_billing.handle(event))
    return HttpResponse(status=200)
