from django.conf import settings
from django.db import connection
from rest_framework import generics, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Item
from .serializers import ItemSerializer


@api_view(["GET"])
def api_index(request):
    """List all APIs so frontend/dev can see Teams routes (not only health)."""
    return Response(
        {
            "message": "AIDL API index",
            "microsoft_redirect_uri": settings.MS_REDIRECT_URI,
            "auth_success_redirect": settings.AUTH_SUCCESS_REDIRECT,
            "aidl_channel": {
                "team_id_configured": bool((settings.MS_AIDL_TEAM_ID or "").strip()),
                "channel_name": settings.MS_AIDL_CHANNEL_NAME or "AIDL",
            },
            "endpoints": {
                "health": request.build_absolute_uri("/api/health/"),
                "teams_login": request.build_absolute_uri(
                    "/api/auth/teams/login/?enroll_as=organization"
                ),
                "teams_callback": request.build_absolute_uri("/api/auth/teams/callback/"),
                "teams_launch": request.build_absolute_uri("/api/auth/teams/launch/"),
                "me": request.build_absolute_uri("/api/auth/me/"),
                "refresh": request.build_absolute_uri("/api/auth/refresh/"),
                "logout": request.build_absolute_uri("/api/auth/logout/"),
                "items": request.build_absolute_uri("/api/items/"),
            },
        }
    )


@api_view(["GET"])
def health_check(request):
    mongo_ok = False
    try:
        connection.ensure_connection()
        mongo_ok = True
    except Exception as exc:  # noqa: BLE001
        mongo_error = str(exc)
    else:
        mongo_error = None

    payload = {
        "status": "ok" if mongo_ok else "degraded",
        "framework": "django",
        "database": settings.MONGO_DB_NAME,
        "mongodb_connected": mongo_ok,
    }
    if mongo_error:
        payload["mongodb_error"] = mongo_error

    return Response(
        payload,
        status=status.HTTP_200_OK if mongo_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


class ItemListCreateView(generics.ListCreateAPIView):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer


class ItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
