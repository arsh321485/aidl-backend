"""Google Workspace-only data."""

from django.db import models


class WorkspaceMember(models.Model):
    """Which step email (mail.STEPS) a person got last, so each one is sent once."""

    user_id = models.CharField(max_length=64, unique=True)  # AIDLUser
    organization_id = models.CharField(max_length=64, db_index=True)
    last_step = models.CharField(max_length=32, blank=True, default="")
    last_sent_at = models.DateTimeField(null=True, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)  # the admin's last "Remind"
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user_id}: {self.last_step}"


class WorkspaceInvite(models.Model):
    """A colleague the admin invited by email (mail.invite), so the Admin Center
    can list people who haven't joined yet and remind them."""

    organization_id = models.CharField(max_length=64, db_index=True)
    email = models.EmailField(db_index=True)
    invited_by_id = models.CharField(max_length=64, blank=True, default="")  # AIDLUser
    invited_at = models.DateTimeField()
    reminded_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.organization_id}: {self.email}"
