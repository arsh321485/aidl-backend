"""Reliable file responses for Teams assets (logo / manifest) on Render."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404


def _aidl_static_dir() -> Path:
    return Path(settings.BASE_DIR) / "api" / "static" / "aidl"


def serve_aidl_logo_png(request):
    path = _aidl_static_dir() / "logo.png"
    if not path.is_file():
        raise Http404("logo.png missing")
    return FileResponse(path.open("rb"), content_type="image/png")


def serve_admin_banner(request, version: str):
    """The static AIDL welcome banner on the Slack Home tab (admin_home.py). The version is in the file name,
    so it can be cached for a long time: a new design gets a new name."""
    path = _aidl_static_dir() / f"admin-banner-{version}.png"
    if not version.isalnum() or not path.is_file():
        raise Http404("banner missing")
    response = FileResponse(path.open("rb"), content_type="image/png")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def serve_aidl_logo_svg(request):
    path = _aidl_static_dir() / "logo.svg"
    if not path.is_file():
        raise Http404("logo.svg missing")
    return FileResponse(path.open("rb"), content_type="image/svg+xml")
