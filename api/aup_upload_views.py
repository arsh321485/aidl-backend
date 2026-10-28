"""Upload your own AUP over the API (the same flow as the Slack popup,
aup_upload_slack.py), for the website. Admins only (Bearer AIDL access JWT).

    POST /api/aup/upload/            multipart `file` → 202 {upload}  (cards written in the background)
    GET  /api/aup/upload/            the newest upload: {upload} with status and, when ready, the 14 cards
    POST /api/aup/upload/publish/    {upload_id} → the cards become the AUP (waiting for approval)
"""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import aup_upload
from .auth_views import JWTAuthentication
from .models import AIDLUser, AupUpload
from .org_service import get_organization_for_user


def _admin_org(request):
    user = request.user
    if getattr(user, "role", "") != AIDLUser.Role.ADMIN:
        return None, Response({"detail": "Only an AIDL admin can do this."}, status=status.HTTP_403_FORBIDDEN)
    org = get_organization_for_user(user)
    if org is None:
        return None, Response({"detail": "No organization."}, status=status.HTTP_400_BAD_REQUEST)
    return org, None


def _payload(upload: AupUpload | None) -> dict | None:
    if upload is None:
        return None
    cards = aup_upload.cards_of(upload)
    return {"id": str(upload.pk), "status": upload.status, "file_name": upload.file_name, "error": upload.error,
            "text_truncated": upload.text_truncated, "created_at": upload.created_at.isoformat(),
            "cards": cards, "warnings": aup_upload.warnings(cards) if cards else {}}


@extend_schema(summary="Upload the organization's AUP; the NVIDIA model writes the 14 cards (Enterprise)",
               request={"multipart/form-data": OpenApiTypes.OBJECT},
               responses={202: OpenApiResponse(OpenApiTypes.OBJECT), 200: OpenApiResponse(OpenApiTypes.OBJECT),
                          400: OpenApiResponse(OpenApiTypes.OBJECT, description='{"file": ["..."]}')})
@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload(request):
    org, error = _admin_org(request)
    if error:
        return error
    if request.method == "GET":
        return Response({"allowed": aup_upload.can_upload(org), "upload": _payload(aup_upload.latest(org))})
    f = request.FILES.get("file")
    if f is None:
        return Response({"file": ["Choose your policy file."]}, status=status.HTTP_400_BAD_REQUEST)
    if f.size > settings.AUP_UPLOAD_MAX_BYTES:
        return Response({"file": ["That file is too big."]}, status=status.HTTP_400_BAD_REQUEST)
    try:
        up = aup_upload.start(org, request.user, f.name, f.read())
    except aup_upload.UploadError as exc:
        return Response({"file": [exc.message]}, status=status.HTTP_400_BAD_REQUEST)
    up.refresh_from_db()
    return Response({"upload": _payload(up)}, status=status.HTTP_202_ACCEPTED)


@extend_schema(summary="Use the uploaded AUP's cards as the organization's AUP",
               request=OpenApiTypes.OBJECT, responses={200: OpenApiResponse(OpenApiTypes.OBJECT)})
@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def publish(request):
    org, error = _admin_org(request)
    if error:
        return error
    up = AupUpload.objects.filter(pk=str(request.data.get("upload_id") or ""), organization_id=str(org.pk)).first()
    if up is None:
        return Response({"detail": "Upload not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        aup = aup_upload.publish(org, up, request.user)
    except aup_upload.UploadError as exc:
        return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
    try:
        from .slack_blocks import publish_admin_center

        publish_admin_center(org, request.user)
    except Exception:  # noqa: BLE001 — the AUP is published either way
        pass
    return Response({"ok": True, "version": aup["version"] if aup else "", "upload": _payload(up)})
