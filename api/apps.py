from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "api"

    def ready(self):
        from . import schema  # noqa: F401  registers the Swagger JWT auth scheme

        from django.conf import settings

        if getattr(settings, "FORCE_IPV4_OUTBOUND", False):
            import socket

            import urllib3.util.connection

            urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
