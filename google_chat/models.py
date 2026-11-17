"""Google Chat install for one organization. Member rows come with user onboarding."""

from django.db import models


class GoogleChatInstall(models.Model):
    """One Google Workspace domain connected to one AIDL organization.

    The admin's Chat token and the AIDL space live here, so Organization does
    not gain Google columns.
    """

    organization_id = models.CharField(max_length=64, unique=True)
    hosted_domain = models.CharField(max_length=255, unique=True)
    access_token = models.TextField(blank=True, default="")
    refresh_token = models.TextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    space_name = models.CharField(max_length=128, blank=True, default="")
    space_url = models.CharField(max_length=500, blank=True, default="")
    home_message_name = models.CharField(max_length=256, blank=True, default="")
    admin_google_sub = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.hosted_domain or self.organization_id
