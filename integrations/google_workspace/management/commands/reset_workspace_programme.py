"""Start a company's AIDL programme again from scratch (for testing).

    python manage.py reset_workspace_programme aidrivinglicence.ai          # shows what would change
    python manage.py reset_workspace_programme aidrivinglicence.ai --yes    # does it

Keeps the company, its plan and every account (the admin stays admin). Clears: "programme started",
the two starter-card approvals, everyone's progress (policy agreements, checks, welcome, licence,
avatar, weekly awareness cards) and the Google Workspace email / invite history. The admin then
approves and starts again from the AIDL panel in Gmail, and everyone goes through every step again.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

PROGRESS_FIELDS = {
    "slack_onboarded_at": None, "licence_issued": False, "licence_number": "", "licence_issued_at": None,
    "licence_expires_at": None, "avatar_data": "", "avatar_config": "", "avatar_url": "", "avatar_changed_at": None,
    "aup_signed": False, "aup_signed_at": None,
}


class Command(BaseCommand):
    help = "Reset a company's AIDL programme (approvals, start, everyone's progress). Keeps accounts and plan."

    def add_arguments(self, parser):
        parser.add_argument("domain", help="the company's Google domain, e.g. acme.com")
        parser.add_argument("--yes", action="store_true", help="really reset (without it: only show what would change)")
        parser.add_argument("--policy", action="store_true",
                            help="also remove the company's AI policy cards, so the admin chooses them again")

    def handle(self, domain: str, yes: bool, policy: bool = False, **_):
        from api import admin_setup, aup_store
        from api.card_approval import ONBOARDING
        from api.models import Acknowledgement, AIDLUser, CardApproval, PackageDelivery

        from ...accounts import organization_for_domain
        from ...models import WorkspaceInvite, WorkspaceMember

        org = organization_for_domain(domain.strip().lower())
        if org is None:
            raise CommandError(f"No AIDL company for {domain}.")
        oid = str(org.pk)
        users = AIDLUser.objects.filter(organization_id=org.pk)
        work = {
            "starter-card approvals": CardApproval.objects.filter(organization_id=oid, card_key__in=list(ONBOARDING)),
            "agreements / completed steps": Acknowledgement.objects.filter(organization_id=oid),
            "weekly awareness deliveries": PackageDelivery.objects.filter(organization_id=oid),
            "Workspace email records": WorkspaceMember.objects.filter(organization_id=oid),
            "Workspace invites": WorkspaceInvite.objects.filter(organization_id=oid),
        }
        self.stdout.write(f"{org.name} ({oid}): {users.count()} people, "
                          f"{users.filter(licence_issued=True).count()} licensed, "
                          f"programme {'started' if admin_setup.started(org) else 'not started'}")
        for label, qs in work.items():
            self.stdout.write(f"  {label}: {qs.count()}")
        if policy:
            self.stdout.write(f"  AI policy cards: {len(aup_store.cards(org))} (removed, with their answers)")
        if not yes:
            self.stdout.write("Nothing changed. Run again with --yes to reset.")
            return

        with transaction.atomic():
            for qs in work.values():
                qs.delete()
            users.update(**PROGRESS_FIELDS)
            state = admin_setup.state(org)  # keep the rest of the setup (only "started" goes)
            state.pop("flow_started", None)
            type(org).objects.filter(pk=org.pk).update(setup_state=json.dumps(state))
            if policy:
                org.refresh_from_db()
                aup_store.clear(org)
        org.refresh_from_db()
        self.stdout.write(self.style.SUCCESS(
            f"Reset. Programme {'started' if admin_setup.started(org) else 'not started'}; "
            f"{users.filter(licence_issued=True).count()} licensed. Approve and start again from the AIDL panel."))
