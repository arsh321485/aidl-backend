"""Signed links for AIDL's Gmail emails: a button in an email opens that person's
AIDL step already signed in, with no second Google sign-in.

    /api/google-workspace/go/?t=<signed token>

The token names the person and where to go (a path inside /api/google-workspace/
only), and expires after LINK_DAYS. It is checked against the person's current
state on every use (switched-off accounts can't use it).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt
from django.conf import settings
from django.urls import reverse

from api.models import AIDLUser

LINK_DAYS = 14
TOKEN_TYPE = "google_workspace_email"
PREFIX = "/api/google-workspace/"


def link(user: AIDLUser, path: str = "") -> str:
    """A full URL for an email button. `path` defaults to the person's AIDL home."""
    path = path or reverse("google-workspace-home")
    if not path.startswith(PREFIX):
        raise ValueError(f"email links only open Google Workspace pages, not {path}")
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": str(user.pk), "type": TOKEN_TYPE, "next": path, "iat": now,
                        "exp": now + timedelta(days=LINK_DAYS)}, settings.JWT_SECRET, algorithm="HS256")
    return f"{settings.BACKEND_URL.rstrip('/')}{reverse('google-workspace-go')}?{urlencode({'t': token})}"


def open_link(token: str) -> tuple[AIDLUser | None, str]:
    """(person, path) from a link's token, or (None, "") when it's invalid or expired."""
    try:
        data = jwt.decode(token or "", settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None, ""
    path = str(data.get("next") or "")
    if data.get("type") != TOKEN_TYPE or not path.startswith(PREFIX) or "//" in path:
        return None, ""
    return AIDLUser.objects.filter(pk=data.get("sub"), is_active=True).first(), path
