"""The signed-in cookie for AIDL's Google Workspace pages (like moodle/session.py).

Set after Google proved who the person is. Holds only the user id; role and
organization are read from the database on every request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from django.conf import settings

from api.models import AIDLUser

COOKIE = "aidl_gworkspace"
COOKIE_PATH = "/api/google-workspace/"
SESSION_DAYS = 14
TOKEN_TYPE = "google_workspace_app"


def set_session(request, response, user: AIDLUser):
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": str(user.pk), "type": TOKEN_TYPE, "iat": now, "exp": now + timedelta(days=SESSION_DAYS)},
                       settings.JWT_SECRET, algorithm="HS256")
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=request.is_secure() or not settings.DEBUG, samesite="Lax", path=COOKIE_PATH)
    return response


def clear_session(response):
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    return response


def signed_in(request) -> AIDLUser | None:
    try:
        data = jwt.decode(request.COOKIES.get(COOKIE, ""), settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if data.get("type") != TOKEN_TYPE:
        return None
    return AIDLUser.objects.filter(pk=data.get("sub"), is_active=True).first()
