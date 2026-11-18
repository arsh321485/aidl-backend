"""Hand Slack the awareness cards that are now within its 120-day
scheduling window (a year of Basic / Enterprise cards can't be scheduled at once),
close finished Trials (trial.py), and remind admins about cards waiting for approval
(card_approval.remind). Run daily, e.g. a Render cron job:

    python manage.py schedule_package_cards

Teams has its own job, run hourly: python manage.py teams_dispatch
"""

from django.core.management.base import BaseCommand

from api.card_ack import send_overdue_alerts, send_reminders
from api.card_approval import remind
from api.models import Organization
from api.package_delivery import schedule_due
from api.trial import close_finished_trials


class Command(BaseCommand):
    help = "Schedule saved awareness cards that are now within Slack's 120-day window."

    def handle(self, *args, **options):
        closed = close_finished_trials()
        if closed:
            self.stdout.write(f"{closed} Trial(s) closed")
        for org in Organization.objects.filter(is_active=True).exclude(slack_bot_token=""):
            n = schedule_due(org)
            if n:
                self.stdout.write(f"{org.name}: {n} card(s) scheduled")
            nudged = send_reminders(org)
            if nudged:
                self.stdout.write(f"{org.name}: {nudged} card reminder(s) sent")
            alerted = send_overdue_alerts(org)
            if alerted:
                self.stdout.write(f"{org.name}: {alerted} overdue alert(s) sent")
            told = remind(org)
            if told:
                self.stdout.write(f"{org.name}: {told} admin(s) reminded about cards waiting for approval")
