"""Send every Cards-catalogue CardRequest whose scheduled time has arrived.

Django has no built-in way to run code at a future time by itself, so this
command is meant to be triggered periodically by an external scheduler (a
Render Cron Job, a GitHub Action on a schedule, cron, Windows Task
Scheduler, ...) — e.g. every 5 minutes:

    python manage.py dispatch_scheduled_cards
"""

from django.core.management.base import BaseCommand

from api.cards_service import dispatch_due_scheduled_cards


class Command(BaseCommand):
    help = "Send Cards-catalogue requests whose scheduled_at has arrived."

    def handle(self, *args, **options):
        result = dispatch_due_scheduled_cards()
        self.stdout.write(
            self.style.SUCCESS(
                f"checked={result['checked']} sent={result['sent']} failed={result['failed']}"
            )
        )
