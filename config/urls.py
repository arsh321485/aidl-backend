from django.contrib import admin
from django.urls import include, path
from django.views.static import serve

from django.conf import settings
from pathlib import Path

BASE_DIR = Path(settings.BASE_DIR)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("api.urls")),
    path(
        "teams/manifest.json",
        serve,
        {
            "document_root": BASE_DIR / "teams",
            "path": "manifest.json",
        },
        name="teams-manifest",
    ),
    path(
        "static/aidl/logo.svg",
        serve,
        {
            "document_root": BASE_DIR / "api" / "static" / "aidl",
            "path": "logo.svg",
        },
        name="aidl-logo-svg",
    ),
    path(
        "static/aidl/logo.png",
        serve,
        {
            "document_root": BASE_DIR / "api" / "static" / "aidl",
            "path": "logo.png",
        },
        name="aidl-logo",
    ),
]
