from django.apps import AppConfig


class GoogleChatConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "google_chat"
    verbose_name = "Google Chat"
