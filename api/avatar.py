"""The avatar on the AI Driving License.

When an employee earns their licence in Slack and hasn't picked an avatar,
Slack asks them to (employee_flow.avatar_card):
- "Create my avatar" opens GET /api/avatar/start/?u=<user id>&s=<signature>,
  which sends them to the website's avatar builder (frontend /avatar) with a
  short-lived avatar-only token; the page posts the PNG to /api/avatar/.
Once saved, the licence card (with the avatar) is posted to their Slack DM.

AIDLUser.avatar_data holds the PNG (base64), avatar_config the builder's
settings so the avatar can be edited later.

Creating the first avatar is free and it can then be changed once, in Slack
and Teams (AIDLUser.avatar_changed_at). Old links still reach the page, so the
save is what enforces it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
from datetime import datetime, timedelta, timezone

import jwt
from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone as django_timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from PIL import Image
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import AIDLUser

logger = logging.getLogger(__name__)
MAX_UPLOAD = 2 * 1024 * 1024
TOKEN_HOURS = 24
ALREADY_CHANGED = "You've already changed your avatar once, so it can't be changed again."


def _signature(user_id: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"avatar:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]


def start_url(member: AIDLUser) -> str:
    """The link on the Slack card — it doesn't expire; each click makes a
    fresh token."""
    uid = str(member.pk)
    return f"{settings.BACKEND_URL}/api/avatar/start/?u={uid}&s={_signature(uid)}"


def _token(member: AIDLUser) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(member.pk), "type": "avatar", "iat": now, "exp": now + timedelta(hours=TOKEN_HOURS)},
                      settings.JWT_SECRET, algorithm="HS256")


def _member_for(token: str) -> AIDLUser | None:
    try:
        data = jwt.decode(token or "", settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if data.get("type") != "avatar":
        return None
    return AIDLUser.objects.filter(pk=data.get("sub"), is_active=True).first()


def normalise(raw: bytes) -> str:
    """Any image → a ≤512px PNG, base64. Raises ValueError if it isn't one."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("not an image") from exc
    img = img.convert("RGBA")
    img.thumbnail((512, 512), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def save(member: AIDLUser, png_b64: str, config: dict | None = None) -> None:
    member.avatar_data = png_b64
    member.avatar_config = json.dumps(config) if config else ""
    member.save(update_fields=["avatar_data", "avatar_config", "updated_at"])


def locked(member: AIDLUser) -> bool:
    """The avatar can't be changed any more."""
    return bool(member.avatar_data) and member.avatar_changed_at is not None


def can_change(member: AIDLUser) -> bool:
    """There's an avatar, and its one change hasn't been used."""
    return bool(member.avatar_data) and not locked(member)


def _claim_change(member: AIDLUser) -> bool:
    """Use up the one change. Only one of two saves at the same time gets it."""
    now = django_timezone.now()
    if not AIDLUser.objects.filter(pk=member.pk, avatar_changed_at__isnull=True).update(avatar_changed_at=now):
        return False
    member.avatar_changed_at = now
    return True


def _after_save(member: AIDLUser) -> None:
    """Licensed Slack employees get their licence card (with the avatar)."""
    from .employee_flow import post_licence

    if member.licence_issued and member.microsoft_id.startswith("slack:"):
        post_licence(member)


def _return_url(member: AIDLUser) -> str:
    """Teams people go back to their AIDL page; Slack people just close the tab."""
    if member.microsoft_id.startswith("slack:") or member.microsoft_id.startswith("local:"):
        return ""
    return f"{settings.BACKEND_URL}/api/teams/app/me/"


# ---------- views ----------

def avatar_start(request):
    uid = request.GET.get("u", "")
    if not hmac.compare_digest(request.GET.get("s", ""), _signature(uid)):
        return redirect(f"{settings.FRONTEND_URL.rstrip('/')}/avatar?error=link")
    member = AIDLUser.objects.filter(pk=uid, is_active=True).first()
    if member is None:
        return redirect(f"{settings.FRONTEND_URL.rstrip('/')}/avatar?error=link")
    return redirect(f"{settings.FRONTEND_URL.rstrip('/')}/avatar?token={_token(member)}")


@extend_schema(summary="Avatar for the licence: read (GET ?token=) or save (POST {token, png, config})",
               request=OpenApiTypes.OBJECT, responses={200: OpenApiResponse(OpenApiTypes.OBJECT),
                                                        400: OpenApiResponse(OpenApiTypes.OBJECT),
                                                        401: OpenApiResponse(OpenApiTypes.OBJECT)})
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def avatar_api(request):
    token = request.GET.get("token") if request.method == "GET" else (request.data or {}).get("token")
    member = _member_for(token or "")
    if member is None:
        return Response({"detail": "This link has expired. Open “Create my avatar” again."},
                        status=status.HTTP_401_UNAUTHORIZED)
    if request.method == "GET":
        try:
            config = json.loads(member.avatar_config) if member.avatar_config else None
        except ValueError:
            config = None
        return Response({"name": member.full_name or member.email, "has_avatar": bool(member.avatar_data),
                         "config": config, "licensed": member.licence_issued, "return_url": _return_url(member),
                         "locked": locked(member)})
    if locked(member):
        return Response({"detail": ALREADY_CHANGED}, status=status.HTTP_409_CONFLICT)
    raw = str(request.data.get("png") or "")
    if "," in raw[:100]:
        raw = raw.split(",", 1)[1]  # data:image/png;base64,...
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        data = b""
    if not data or len(data) > MAX_UPLOAD:
        return Response({"png": ["Send the avatar as a PNG under 2 MB."]}, status=status.HTTP_400_BAD_REQUEST)
    try:
        png = normalise(data)
    except ValueError:
        return Response({"png": ["That isn't an image."]}, status=status.HTTP_400_BAD_REQUEST)
    if member.avatar_data and not _claim_change(member):
        return Response({"detail": ALREADY_CHANGED}, status=status.HTTP_409_CONFLICT)
    config = request.data.get("config")
    save(member, png, config if isinstance(config, dict) else None)
    _after_save(member)
    if member.microsoft_id.startswith("slack:"):
        message = "Your avatar is saved — your AI Driving License is in Slack."
    else:
        message = "Your avatar is saved — your AI Driving Licence is ready in AIDL."
    return Response({"ok": True, "licensed": member.licence_issued, "return_url": _return_url(member),
                     "locked": locked(member), "message": message if member.licence_issued else "Your avatar is saved."})
