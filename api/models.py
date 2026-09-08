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


class Organization(models.Model):
    """Tenant / company running AIDL Admin Center."""

    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=128, blank=True, default="", unique=True)
    teams_team_id = models.CharField(max_length=128, blank=True, default="")
    seats_purchased = models.IntegerField(default=50)
    seats_renews_on = models.DateField(null=True, blank=True)
    admin_seat_limit = models.IntegerField(default=3)
    rollout_steps_done = models.IntegerField(default=3)
    rollout_steps_total = models.IntegerField(default=4)
    policy_title = models.CharField(
        max_length=255,
        blank=True,
        default="Acceptable Use of Technology Policy",
    )
    policy_url = models.URLField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    # Guards the Teams channel "Admin Center" Adaptive Card against being
    # re-posted (duplicated) on every login — set True once it has been
    # posted for this channel; cleared when the channel is recreated.
    admin_card_sent = models.BooleanField(default=False)
    admin_card_channel_id = models.CharField(max_length=255, blank=True, default="")
    admin_card_message_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AIDLUser(models.Model):
    """App user created via Teams / Microsoft OAuth."""

    class EnrollAs(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        ORGANIZATION = "organization", "Organization"

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        LEARNER = "learner", "Learner"

    microsoft_id = models.CharField(max_length=255, unique=True)
    email = models.EmailField(blank=True, default="")
    full_name = models.CharField(max_length=255, blank=True, default="")
    enroll_as = models.CharField(
        max_length=32,
        choices=EnrollAs.choices,
        default=EnrollAs.INDIVIDUAL,
    )
    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.LEARNER,
    )
    organization_id = models.CharField(max_length=64, blank=True, default="")
    organization_name = models.CharField(max_length=255, blank=True, default="")
    provider = models.CharField(max_length=32, default="teams")
    avatar_url = models.URLField(blank=True, default="")
    teams_team_id = models.CharField(max_length=128, blank=True, default="")
    teams_channel_id = models.CharField(max_length=255, blank=True, default="")
    teams_channel_name = models.CharField(max_length=128, blank=True, default="")
    licence_issued = models.BooleanField(default=False)
    aup_signed = models.BooleanField(default=False)
    aup_signed_at = models.DateTimeField(null=True, blank=True)
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


class RegisteredApp(models.Model):
    """AI / IT application in an organisation registry."""

    class AppType(models.TextChoices):
        AI = "ai", "AI"
        IT = "it", "IT"

    class Status(models.TextChoices):
        APPROVED = "approved", "Approved"
        PENDING = "pending", "Pending"
        REJECTED = "rejected", "Rejected"

    organization_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=255)
    app_type = models.CharField(max_length=16, choices=AppType.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CardDelivery(models.Model):
    """
    Per-organisation state for one catalog card (see api/card_catalog.py).
    One row per (organization_id, card_key) — re-fetched on every tab load
    so re-opening the Cards tab (or the same person logging in again) shows
    the real current state instead of resetting to "not sent".
    """

    class Status(models.TextChoices):
        NOT_SENT = "not_sent", "Not sent"
        SENT = "sent", "Sent"
        SCHEDULED = "scheduled", "Scheduled"

    organization_id = models.CharField(max_length=64, db_index=True)
    card_key = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.NOT_SENT,
    )
    purchased = models.BooleanField(default=False)
    lights = models.CharField(max_length=32, blank=True, default="")  # csv: green,amber,red
    scheduled_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["card_key"]

    def __str__(self):
        return f"{self.organization_id}:{self.card_key}={self.status}"


class CardRequest(models.Model):
    """A custom reference card requested by an org admin (not in the fixed catalog)."""

    class Priority(models.TextChoices):
        STANDARD = "standard", "Standard"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending Review"
        SCOPED = "scoped", "Scoped"
        DONE = "done", "Delivered"

    organization_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    priority = models.CharField(
        max_length=16,
        choices=Priority.choices,
        default=Priority.STANDARD,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    requested_by_email = models.EmailField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


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
