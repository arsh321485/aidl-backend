"""Internal promo codes for testing paid plans without paying (billing.py).

    python manage.py promo_code create --label "QA Nov" --email a@company.com --email b@company.com
    python manage.py promo_code create --label "QA Basic" --email a@company.com --plan basic --days 7 --max-uses 2
    python manage.py promo_code list
    python manage.py promo_code disable --label "QA Nov"

The code is shown once, when it's created, and only its hash is saved. Only
the people named by --email can use it, and only until it expires. It changes
no real subscription: organizations that pay are refused.
"""

import secrets
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.utils import timezone

from api.billing import promo_hash
from api.models import PromoCode
from api.plans import ORDER


class Command(BaseCommand):
    help = "Create, list or disable internal promo codes."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        create = actions.add_parser("create")
        create.add_argument("--label", required=True, help="a name for this code (never the code itself)")
        create.add_argument("--email", action="append", required=True, help="who may use it (repeat for each person)")
        create.add_argument("--plan", choices=[p for p in ORDER if p != "trial"], default="enterprise",
                            help="the highest plan it opens")
        create.add_argument("--days", type=int, default=30, help="days until it expires")
        create.add_argument("--max-uses", type=int, default=10)
        actions.add_parser("list")
        disable = actions.add_parser("disable")
        disable.add_argument("--label", required=True)

    def handle(self, *args, **opts):
        if opts["action"] == "create":
            self._create(opts)
        elif opts["action"] == "list":
            for row in PromoCode.objects.all():
                self.stdout.write(f"{row.label} | {row.plan} | {row.allowed_emails} | used {row.uses}/{row.max_uses} | "
                                  f"expires {row.expires_at:%d %b %Y} | {'active' if row.is_active else 'disabled'}")
        elif not PromoCode.objects.filter(label=opts["label"]).update(is_active=False):
            raise CommandError("No code with that label.")
        else:
            self.stdout.write("Disabled. Plans already activated with it don't change; use set_plan for those.")

    def _create(self, opts):
        emails = sorted({e.strip().lower() for e in opts["email"]})
        for email in emails:
            try:
                validate_email(email)
            except ValidationError as exc:
                raise CommandError(f"Not an email address: {email}") from exc
        if opts["days"] < 1 or opts["max_uses"] < 1:
            raise CommandError("--days and --max-uses must be at least 1.")
        if PromoCode.objects.filter(label=opts["label"]).exists():
            raise CommandError("That label is already used.")
        code = "AIDL-" + secrets.token_hex(16).upper()
        PromoCode.objects.create(code_hash=promo_hash(code), label=opts["label"], plan=opts["plan"],
                                 allowed_emails=",".join(emails), max_uses=opts["max_uses"],
                                 expires_at=timezone.now() + timedelta(days=opts["days"]))
        self.stdout.write(f"{code}\nSave it now: it can't be shown again. For: {', '.join(emails)}")
