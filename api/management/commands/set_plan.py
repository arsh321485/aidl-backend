"""Change an organization's AIDL plan (there's no payment flow yet).

    python manage.py set_plan --team T0123 --plan basic --seats 50
    python manage.py set_plan --team T0123 --plan trial
    python manage.py set_plan --team T0123            # just show the plan
    python manage.py set_plan --team T0123 --owner shivraj@company.com
        # make that admin the organization's first admin (Add Admin, remove admins)

Licensed learners' cards are re-scheduled for the new package: cards they
already got are kept, the rest follow the new package's posting order.
People already in the channel stay even if the new limit is lower; new
joins are refused until the team is under the limit.
"""

from django.core.management.base import BaseCommand, CommandError

from api.models import AIDLUser, Organization
from api.package_delivery import sync_organization
from api.plans import PLANS, usage


class Command(BaseCommand):
    help = "Show or change an organization's AIDL plan (trial / basic)."

    def add_arguments(self, parser):
        parser.add_argument("--team", required=True, help="Slack workspace (team) id")
        parser.add_argument("--plan", choices=sorted(PLANS), default="")
        parser.add_argument("--seats", type=int, default=0, help="Basic plan: number of users")
        parser.add_argument("--owner", default="", help="email of the admin who owns the organization")

    def handle(self, *args, **options):
        org = Organization.objects.filter(slack_team_id=options["team"]).first()
        if org is None:
            raise CommandError(f"No organization for Slack workspace {options['team']}")
        changed = []
        if options["plan"] and options["plan"] != org.plan:
            org.plan = options["plan"]
            changed.append("plan")
        if options["owner"]:
            owner = AIDLUser.objects.filter(organization_id=str(org.pk), email__iexact=options["owner"],
                                            role=AIDLUser.Role.ADMIN, is_active=True).first()
            if owner is None:
                raise CommandError(f"{options['owner']} is not an active admin of {org.name}")
            org.owner_user_id = str(owner.pk)
            changed.append("owner_user_id")
        if options["seats"]:
            org.seats_purchased = options["seats"]
            changed.append("seats_purchased")
        if changed:
            org.save(update_fields=[*changed, "updated_at"])
            if "plan" in changed:
                self.stdout.write(f"Cards re-scheduled: {sync_organization(org)}")
        u = usage(org)
        self.stdout.write(f"{org.name}: {u['label']} plan · {u['used']} of {u['limit']} users · "
                          f"{u['package_label']} ({u['cards']} cards, {u['cards_per_week']}/week)")
