from django.contrib import admin
from django.urls import include, path
from django.views.static import serve

from django.conf import settings
from pathlib import Path

from api.static_views import serve_aidl_logo_png, serve_aidl_logo_svg

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
    # FileResponse — works with DEBUG=False on Render (django.views.static.serve is flaky).
    path("static/aidl/logo.png", serve_aidl_logo_png, name="aidl-logo"),
    path("static/aidl/logo.svg", serve_aidl_logo_svg, name="aidl-logo-svg"),
]
