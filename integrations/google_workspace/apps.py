from django.apps import AppConfig


class GoogleWorkspaceConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "integrations.google_workspace"
    label = "google_workspace"
    verbose_name = "Google Workspace"
