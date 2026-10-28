"""python manage.py export_waitlist [--out waitlist.csv]

The website waitlist (WaitlistEntry) as CSV: email, source, joined (UTC).
Without --out it prints to the screen."""

import csv

from django.core.management.base import BaseCommand

from api.models import WaitlistEntry


class Command(BaseCommand):
    help = "Export the website waitlist as CSV."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="", help="CSV file to write (default: print)")

    def handle(self, *args, **options):
        rows = WaitlistEntry.objects.order_by("created_at")
        fh = open(options["out"], "w", newline="", encoding="utf-8") if options["out"] else self.stdout
        try:
            writer = csv.writer(fh)
            writer.writerow(["email", "source", "joined_utc"])
            for r in rows:
                writer.writerow([r.email, r.source, r.created_at.strftime("%Y-%m-%d %H:%M")])
        finally:
            if options["out"]:
                fh.close()
                self.stderr.write(f"{rows.count()} people written to {options['out']}")
