from django.db import models


class Item(models.Model):
    """Sample MongoDB-backed model."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class AIDLUser(models.Model):
    """App user created via Teams / Microsoft OAuth."""

    class EnrollAs(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        ORGANIZATION = "organization", "Organization"

    microsoft_id = models.CharField(max_length=255, unique=True)
    email = models.EmailField(blank=True, default="")
    full_name = models.CharField(max_length=255, blank=True, default="")
    enroll_as = models.CharField(
        max_length=32,
        choices=EnrollAs.choices,
        default=EnrollAs.INDIVIDUAL,
    )
    provider = models.CharField(max_length=32, default="teams")
    avatar_url = models.URLField(blank=True, default="")
    # Cached Teams channel deep-link targets (set after Graph ensure on login)
    teams_team_id = models.CharField(max_length=128, blank=True, default="")
    teams_channel_id = models.CharField(max_length=255, blank=True, default="")
    teams_channel_name = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def __str__(self):
        return self.email or self.microsoft_id


class OAuthState(models.Model):
    """Short-lived CSRF state for Microsoft OAuth."""

    state = models.CharField(max_length=128, unique=True)
    enroll_as = models.CharField(max_length=32, default="individual")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.state
