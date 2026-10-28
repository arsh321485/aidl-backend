"""Website waitlist for individuals ("Not a company? Join the waitlist").

    POST /api/waitlist/  {email, source?} → {ok: true}

Public, no token. The same email twice is kept once (and still answers ok,
so the box doesn't tell anyone which emails are already on the list).
Every new sign-up is emailed to WAITLIST_NOTIFY_EMAIL (else
BILLING_SALES_EMAIL). The list: `python manage.py export_waitlist`.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import WaitlistEntry

logger = logging.getLogger(__name__)


@extend_schema(
    summary="Join the individual waitlist (website)",
    request=OpenApiTypes.OBJECT,
    responses={200: OpenApiResponse(OpenApiTypes.OBJECT, description='{"ok": true}'),
               400: OpenApiResponse(OpenApiTypes.OBJECT, description='{"email": ["..."]}')},
)
@api_view(["POST"])
@permission_classes([AllowAny])
def join(request):
    data = request.data if isinstance(request.data, dict) else {}
    email = str(data.get("email") or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return Response({"email": ["Enter a valid email address."], "detail": "Enter a valid email address."},
                        status=status.HTTP_400_BAD_REQUEST)
    source = str(data.get("source") or "").strip()[:40]
    _entry, created = WaitlistEntry.objects.get_or_create(email=email, defaults={"source": source})
    if created:
        _notify(email, source)
    return Response({"ok": True})


def _notify(email: str, source: str) -> None:
    to = settings.WAITLIST_NOTIFY_EMAIL or settings.BILLING_SALES_EMAIL
    if not to:
        return
    from django.core.mail import send_mail

    try:
        total = WaitlistEntry.objects.count()
        send_mail(subject=f"AIDL waitlist: {email}",
                  message=f"{email} joined the AIDL waitlist (from: {source or 'website'}).\n"
                          f"{total} people are on the waitlist now.",
                  from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[to], fail_silently=True)
    except Exception as exc:  # noqa: BLE001 — the sign-up is saved either way
        logger.warning("waitlist email: %s", exc)
