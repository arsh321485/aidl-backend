"""JWT helpers for AIDL auth."""

from datetime import datetime, timedelta, timezone

import jwt
from django.conf import settings


def _now():
    return datetime.now(timezone.utc)


def create_access_token(user) -> str:
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "enroll_as": user.enroll_as,
        "type": "access",
        "exp": _now() + timedelta(minutes=settings.JWT_ACCESS_MINUTES),
        "iat": _now(),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def create_refresh_token(user) -> str:
    payload = {
        "sub": str(user.id),
        "type": "refresh",
        "exp": _now() + timedelta(days=settings.JWT_REFRESH_DAYS),
        "iat": _now(),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
