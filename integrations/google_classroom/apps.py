from django.apps import AppConfig


class GoogleClassroomConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "integrations.google_classroom"
    label = "google_classroom"
    verbose_name = "Google Classroom"

    def ready(self):
        """Google Classroom doesn't tell apps when a student answers a card, so
        while the server runs, check every GOOGLE_CLASSROOM_POLL_SECONDS
        (0 = off; on Render use `manage.py google_classroom_sync` from a cron
        job instead, so several workers don't all poll)."""
        import os
        import sys

        seconds = int(os.getenv("GOOGLE_CLASSROOM_POLL_SECONDS", "0") or 0)
        serving = "runserver" in sys.argv and os.environ.get("RUN_MAIN") == "true"  # not the autoreloader parent
        if seconds > 0 and (serving or "gunicorn" in sys.argv[0]):
            from .poller import start

            start(seconds)
