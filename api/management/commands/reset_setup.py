"""Start an organization's step-by-step Slack setup again (for demos).

    python manage.py reset_setup --team T0123
        # forgets the AUP answers, which learning cards were sent, and
        # everyone's AUP acceptance; Highway Code / Traffic Light acceptances
        # and licences are kept
    python manage.py reset_setup --team T0123 --keep-answers
        # same, but keeps the 8 policy answers
"""

from django.core.management.base import BaseCommand, CommandError

from api.admin_setup import reset
from api.models import Organization


class Command(BaseCommand):
    help = "Reset the Slack Admin Center setup steps (AUP, sent cards) for one organization."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Slack workspace (team) id")
        parser.add_argument("--keep-answers", action="store_true", help="keep the 8 policy answers")

    def handle(self, *args, **options):
        org = Organization.objects.filter(slack_team_id=options["team"]).first()
        if org is None:
            raise CommandError(f"No organization for Slack workspace {options['team']}")
        result = reset(org, answers=not options["keep_answers"])
        self.stdout.write(f"{org.name}: {result}")
        from api.slack_blocks import publish_admin_center
        from api.slack_onboarding import primary_admin

        admin = primary_admin(org)
        if admin is not None:
            self.stdout.write(f"Admin Center refreshed: {publish_admin_center(org, admin)}")
