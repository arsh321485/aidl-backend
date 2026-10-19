"""Hand Slack the awareness cards that are now within its 120-day
scheduling window (Trial sends one card a month, so a year of cards can't
be scheduled at once), and remind admins about cards waiting for approval
(card_approval.remind). Run daily, e.g. a Render cron job:

    python manage.py schedule_package_cards
"""

from django.core.management.base import BaseCommand

from api.card_approval import remind
from api.models import Organization
from api.package_delivery import schedule_due


class Command(BaseCommand):
    help = "Schedule saved awareness cards that are now within Slack's 120-day window."

    def handle(self, *args, **options):
        for org in Organization.objects.filter(is_active=True).exclude(slack_bot_token=""):
            n = schedule_due(org)
            if n:
                self.stdout.write(f"{org.name}: {n} card(s) scheduled")
            told = remind(org)
            if told:
                self.stdout.write(f"{org.name}: {told} admin(s) reminded about cards waiting for approval")
