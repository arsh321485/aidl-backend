from django.apps import AppConfig


class MoodleConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "integrations.moodle"
    label = "moodle"
    verbose_name = "Moodle"
