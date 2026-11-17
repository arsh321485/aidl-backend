"""The signed-in cookie for AIDL's Moodle pages (like teams_portal's "aidl_teams").

The cookie only holds the user id. Role and organization are read from the
database on every request, so removing an admin takes effect at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from django.conf import settings

from api.models import AIDLUser

COOKIE = "aidl_moodle"
COOKIE_PATH = "/api/moodle/"
SESSION_DAYS = 14
TOKEN_TYPE = "moodle_app"


def set_session(request, response, user: AIDLUser):
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": str(user.pk), "type": TOKEN_TYPE, "iat": now, "exp": now + timedelta(days=SESSION_DAYS)},
                       settings.JWT_SECRET, algorithm="HS256")
    # Moodle may show AIDL inside a frame on its own site; the browser then only
    # sends the cookie back if it is SameSite=None (which needs https).
    secure = request.is_secure() or not settings.DEBUG
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, secure=secure,
                        samesite="None" if secure else "Lax", path=COOKIE_PATH)
    return response


def signed_in(request) -> AIDLUser | None:
    try:
        data = jwt.decode(request.COOKIES.get(COOKIE, ""), settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if data.get("type") != TOKEN_TYPE:
        return None
    return AIDLUser.objects.filter(pk=data.get("sub"), is_active=True).first()
