from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "api"

    def ready(self):
        from . import schema  # noqa: F401  registers the Swagger JWT auth scheme
