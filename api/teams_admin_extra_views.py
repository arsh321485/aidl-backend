"""New Admin Center endpoints: Policy upload/versioning, Cards catalogue
(request / send / schedule / request-new), and Add Application for the
AI Apps / IT Apps tabs. Kept out of teams_app_views.py just to keep that
file from growing indefinitely — same auth/response conventions as it."""

from __future__ import annotations

import base64
from datetime import datetime

from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
)
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .admin_ops import AdminOpsError, add_registered_app
from .auth_views import JWTAuthentication
from .cards_service import CardsError, request_card, request_new_card, schedule_card, send_card_now
from .models import AIDLUser, PolicyVersion
from .org_service import get_organization_for_user

# A compliance PDF is realistically well under this; Mongo's own hard cap on
# a single document is 16MB, and base64 inflates size by ~1/3, so this stays
# safely clear of that ceiling.
MAX_POLICY_FILE_BYTES = 8 * 1024 * 1024


def _caller_org(caller):
    org = get_organization_for_user(caller)
    if org is None:
        return None
    return org


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def teams_admin_policy_upload(request):
    """Upload a new Acceptable Use Policy PDF and make it the live version."""
    caller = request.user
    if caller.role != AIDLUser.Role.ADMIN:
        return Response(
            {"error": "not_admin", "message": "Only an admin can publish a policy."},
            status=status.HTTP_403_FORBIDDEN,
        )
    org = _caller_org(caller)
    if org is None:
        return Response({"error": "no_organization"}, status=status.HTTP_400_BAD_REQUEST)

    upload = request.FILES.get("file")
    if upload is None:
        return Response({"error": "file_required"}, status=status.HTTP_400_BAD_REQUEST)
    if upload.size > MAX_POLICY_FILE_BYTES:
        return Response(
            {"error": "file_too_large", "max_bytes": MAX_POLICY_FILE_BYTES},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if upload.content_type not in ("application/pdf", "application/octet-stream") and not upload.name.lower().endswith(".pdf"):
        return Response({"error": "pdf_required"}, status=status.HTTP_400_BAD_REQUEST)

    version = (request.data.get("version") or "").strip()
    effective_date_raw = (request.data.get("effective_date") or "").strip()
    effective_date = None
    if effective_date_raw:
        try:
            effective_date = datetime.strptime(effective_date_raw, "%Y-%m-%d").date()
        except ValueError:
            return Response({"error": "invalid_effective_date"}, status=status.HTTP_400_BAD_REQUEST)

    file_bytes = upload.read()
    PolicyVersion.objects.filter(organization_id=str(org.pk), is_live=True).update(is_live=False)
    new_version = PolicyVersion.objects.create(
        organization_id=str(org.pk),
        file_name=upload.name,
        file_content_type=upload.content_type or "application/pdf",
        file_base64=base64.b64encode(file_bytes).decode("ascii"),
        version=version,
        effective_date=effective_date,
        uploaded_by_email=caller.email,
        uploaded_by_name=caller.full_name,
        is_live=True,
    )
    return Response(
        {
            "ok": True,
            "policy_version_id": str(new_version.pk),
            "file_name": new_version.file_name,
            "version": new_version.version,
            "effective_date": new_version.effective_date.isoformat() if new_version.effective_date else "",
            "policy_url": f"/api/teams/admin/policy/file/{new_version.pk}/",
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def teams_admin_policy_file(request, version_id: str):
    """Serve a stored policy PDF back out — the base64 round-trip is
    invisible to whoever clicks "View Current Policy"."""
    version = PolicyVersion.objects.filter(pk=version_id).first()
    if version is None:
        return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    try:
        file_bytes = base64.b64decode(version.file_base64)
    except Exception:  # noqa: BLE001
        return Response({"error": "corrupt_file"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    response = HttpResponse(file_bytes, content_type=version.file_content_type or "application/pdf")
    response["Content-Disposition"] = f'inline; filename="{version.file_name}"'
    return response


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_cards_request(request, card_id: str):
    caller = request.user
    if not caller.perm_access_cards:
        return Response(
            {"error": "permission_denied", "message": "This admin does not have Access Cards permission."},
            status=status.HTTP_403_FORBIDDEN,
        )
    org = _caller_org(caller)
    if org is None:
        return Response({"error": "no_organization"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        row = request_card(org, card_id=card_id, by_email=caller.email)
    except CardsError as exc:
        return Response({"error": exc.code, "message": exc.message}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"ok": True, "card_id": card_id, "status": row.status})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_cards_send(request, card_id: str):
    caller = request.user
    if not caller.perm_access_cards:
        return Response(
            {"error": "permission_denied", "message": "This admin does not have Access Cards permission."},
            status=status.HTTP_403_FORBIDDEN,
        )
    org = _caller_org(caller)
    if org is None:
        return Response({"error": "no_organization"}, status=status.HTTP_400_BAD_REQUEST)

    when = (request.data.get("when") or "now").strip().lower()
    if when == "later":
        at_raw = (request.data.get("at") or "").strip()
        scheduled_at = parse_datetime(at_raw) if at_raw else None
        if scheduled_at is None:
            return Response({"error": "invalid_at"}, status=status.HTTP_400_BAD_REQUEST)
        if timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())
        try:
            row = schedule_card(org, card_id=card_id, by_email=caller.email, scheduled_at=scheduled_at)
        except CardsError as exc:
            return Response({"error": exc.code, "message": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True, "card_id": card_id, "status": row.status, "scheduled_at": row.scheduled_at})

    try:
        send_card_now(org, card_id=card_id, by_user=caller)
    except CardsError as exc:
        code = status.HTTP_400_BAD_REQUEST if exc.code != "send_failed" else status.HTTP_502_BAD_GATEWAY
        return Response({"error": exc.code, "message": exc.message}, status=code)
    return Response({"ok": True, "card_id": card_id, "status": "sent"})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_cards_request_new(request):
    caller = request.user
    if not caller.perm_create_card:
        return Response(
            {"error": "permission_denied", "message": "This admin does not have Create Card permission."},
            status=status.HTTP_403_FORBIDDEN,
        )
    org = _caller_org(caller)
    if org is None:
        return Response({"error": "no_organization"}, status=status.HTTP_400_BAD_REQUEST)
    title = (request.data.get("title") or "").strip()
    description = (request.data.get("description") or "").strip()
    try:
        row = request_new_card(org, by_email=caller.email, title=title, description=description)
    except CardsError as exc:
        return Response({"error": exc.code, "message": exc.message}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"ok": True, "id": str(row.pk), "title": row.title})


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def teams_admin_app_add(request, app_type: str):
    """Shared handler for POST /api/teams/admin/ai-apps/add/ and
    /api/teams/admin/it-apps/add/ — the mock-up's "Add Application" form."""
    caller = request.user
    org = _caller_org(caller)
    if org is None:
        return Response({"error": "no_organization"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        app = add_registered_app(
            org,
            app_type=app_type,
            name=request.data.get("name") or "",
            category=request.data.get("category") or "",
            data_allowed=request.data.get("data_allowed") or "",
            status=request.data.get("status") or "",
            description=request.data.get("description") or "",
        )
    except AdminOpsError as exc:
        code = status.HTTP_404_NOT_FOUND if exc.code == "unknown_app_type" else status.HTTP_400_BAD_REQUEST
        return Response({"error": exc.code, "message": exc.message}, status=code)

    return Response(
        {
            "ok": True,
            "app": {
                "name": app.name,
                "status": app.status,
                "category": app.category,
                "data_allowed": app.data_allowed,
            },
        }
    )
