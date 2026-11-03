"""Teams has no scheduled messages, so this posts what's due: awareness cards,
dated policy cards, reminders, and it notices who joined or left the team. Run
hourly, e.g. a Render cron job (or POST /api/teams/app/dispatch/ with
TEAMS_CRON_SECRET from any scheduler):

    python manage.py teams_dispatch
"""

from django.core.management.base import BaseCommand

from api.teams_channel import dispatch_all


class Command(BaseCommand):
    help = "Post the Teams cards that are due and sync each AIDL team's members."

    def handle(self, *args, **options):
        for name, result in dispatch_all().items():
            self.stdout.write(f"{name}: {result}")
