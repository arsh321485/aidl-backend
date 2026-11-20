"""Take back policy cards on paid topics from Trial organizations (Trial = 3 topics only).

    python manage.py retire_paid_policy_cards --team T0123          # dry run: shows what would go
    python manage.py retire_paid_policy_cards --team T0123 --apply  # removes cards, their scheduled messages and "I agree"s
"""

from django.core.management.base import BaseCommand, CommandError

from api import policy_cards
from api.models import Organization


class Command(BaseCommand):
    help = "Remove approved policy cards (and their acknowledgements) on paid topics from a Trial organization."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Slack workspace (team) id")
        parser.add_argument("--apply", action="store_true", help="really remove them (default: only show)")

    def handle(self, *args, **options):
        org = Organization.objects.filter(slack_team_id=options["team"]).first()
        if org is None:
            raise CommandError(f"No organization for Slack workspace {options['team']}")
        r = policy_cards.retire_paid_topics(org, apply=options["apply"])
        self.stdout.write(f"{org.name} · plan {org.plan} · {'REMOVED' if r['applied'] else 'dry run, nothing changed'}")
        for t in r["topics"]:
            self.stdout.write(f"  point {t['no']:>2} {t['title']} ({t['version']}): {t['acknowledgements']} agreed, "
                              f"{t['scheduled']} scheduled")
        if not r["topics"]:
            self.stdout.write("  nothing to remove")
