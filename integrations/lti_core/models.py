"""LTI 1.3 data shared by every LMS (Moodle, Canvas, Schoology, Brightspace, Blackboard).

LtiPlatform   one LMS site that installed AIDL (for example one school's Moodle).
              One platform = one AIDL organization, like one Slack workspace.
LtiLoginState the short-lived "state" + "nonce" of one login, used once.
"""

from django.db import models


class LtiPlatform(models.Model):
    """An LMS registration. The values come from the LMS's tool settings page."""

    class Kind(models.TextChoices):
        MOODLE = "moodle", "Moodle"
        CANVAS = "canvas", "Canvas"
        SCHOOLOGY = "schoology", "Schoology"
        BRIGHTSPACE = "brightspace", "Brightspace"
        BLACKBOARD = "blackboard", "Blackboard"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    name = models.CharField(max_length=255, help_text="A name for us, e.g. 'Green Valley School Moodle'.")
    # Moodle: the site URL, e.g. https://moodle.school.edu (no trailing slash).
    issuer = models.CharField(max_length=255)
    client_id = models.CharField(max_length=255)
    # Comma-separated: an LMS can install the tool more than once.
    deployment_ids = models.CharField(max_length=512, help_text="Comma-separated deployment ids.")
    auth_login_url = models.URLField(max_length=500)
    auth_token_url = models.URLField(max_length=500, blank=True, default="")
    jwks_url = models.URLField(max_length=500)
    # The AIDL organization, set on the first teacher/admin launch.
    organization_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("issuer", "client_id")
        ordering = ["name"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name}"

    def has_deployment(self, deployment_id: str) -> bool:
        allowed = {d.strip() for d in self.deployment_ids.split(",") if d.strip()}
        return bool(deployment_id) and deployment_id in allowed


class LtiLoginState(models.Model):
    """Step 1 of a login saves this; step 2 (the launch) checks it and marks it used."""

    state = models.CharField(max_length=64, unique=True)
    nonce = models.CharField(max_length=64)
    platform_id = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.state
