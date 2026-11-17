from django.apps import AppConfig


class GoogleClassroomConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "integrations.google_classroom"
    label = "google_classroom"
    verbose_name = "Google Classroom"
