"""Register (or update) a Moodle site with AIDL.

    python manage.py moodle_register --site http://localhost:8080 --client-id abc123 --deployment-id 1 --name "Test Moodle"

Moodle's own URLs are always the same, so only the site URL is needed:
    auth  <site>/mod/lti/auth.php
    token <site>/mod/lti/token.php
    keys  <site>/mod/lti/certs.php
"""

from django.core.management.base import BaseCommand

from integrations.lti_core.models import LtiPlatform


class Command(BaseCommand):
    help = "Register a Moodle site (from its AIDL tool settings: client id + deployment id)."

    def add_arguments(self, parser):
        parser.add_argument("--site", required=True, help="Moodle site URL, e.g. https://moodle.school.edu")
        parser.add_argument("--client-id", required=True)
        parser.add_argument("--deployment-id", required=True, help="Comma-separated if more than one")
        parser.add_argument("--name", default="", help="A name for us (default: the site URL)")

    def handle(self, *args, **opts):
        site = opts["site"].rstrip("/")
        platform, created = LtiPlatform.objects.update_or_create(
            issuer=site, client_id=opts["client_id"],
            defaults={
                "kind": LtiPlatform.Kind.MOODLE,
                "name": opts["name"] or site,
                "deployment_ids": opts["deployment_id"],
                "auth_login_url": f"{site}/mod/lti/auth.php",
                "auth_token_url": f"{site}/mod/lti/token.php",
                "jwks_url": f"{site}/mod/lti/certs.php",
                "is_active": True,
            },
        )
        self.stdout.write(self.style.SUCCESS(f"{'Registered' if created else 'Updated'}: {platform} (id {platform.pk})"))
