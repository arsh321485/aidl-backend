"""Onboard people who were already in an organization's AIDL Slack channel
before auto-onboarding was switched on.

    python manage.py onboard_slack_members              # every Slack org
    python manage.py onboard_slack_members --team T0123 # one workspace
"""

from django.core.management.base import BaseCommand

from api.models import Organization
from api.slack_onboarding import onboard_channel_members


class Command(BaseCommand):
    help = "Send AIDL onboarding to existing members of each organization's Slack channel."

    def add_arguments(self, parser):
        parser.add_argument("--team", default="", help="Slack workspace (team) id; default: all")

    def handle(self, *args, **options):
        orgs = Organization.objects.exclude(slack_channel_id="").filter(is_active=True)
        if options["team"]:
            orgs = orgs.filter(slack_team_id=options["team"])
        for org in orgs:
            counts = onboard_channel_members(org)
            self.stdout.write(f"{org.name}: {counts}")
