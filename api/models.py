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
    teams_welcome_channel_id = models.CharField(max_length=255, blank=True, default="")
    teams_welcome_message_id = models.CharField(max_length=255, blank=True, default="")
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
    logo_url = models.URLField(blank=True, default="")
    is_active = models.BooleanField(default=True)
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
    licence_number = models.CharField(max_length=32, blank=True, default="")
    licence_issued_at = models.DateTimeField(null=True, blank=True)
    licence_expires_at = models.DateTimeField(null=True, blank=True)
    aup_signed = models.BooleanField(default=False)
    aup_signed_at = models.DateTimeField(null=True, blank=True)
    # Admin Center "Add Admin" permission chips — set when an admin is
    # promoted/invited, only meaningful for role=admin. Default True so
    # existing admins (promoted before these chips existed) keep working
    # exactly as before instead of silently losing access.
    perm_approve_apps = models.BooleanField(default=True)
    perm_access_cards = models.BooleanField(default=True)
    perm_create_card = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    # Microsoft OAuth refresh token (requires the "offline_access" scope), so
    # the backend can mint a fresh Graph access token on demand — e.g. to add
    # an invited teammate to the Team — without needing this user's browser
    # open. Only stored for users who have logged in since offline_access was
    # added to MS_SCOPES; older sessions must re-login once to populate it.
    ms_refresh_token = models.TextField(blank=True, default="")
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


class Invitation(models.Model):
    """Pending invite for a new (not-yet-signed-in) user to join an
    organisation's AIDL Team as a Learner, sent by an existing admin."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"

    token = models.CharField(max_length=64, unique=True)
    email = models.EmailField()
    full_name = models.CharField(max_length=255, blank=True, default="")
    role = models.CharField(
        max_length=32,
        choices=AIDLUser.Role.choices,
        default=AIDLUser.Role.LEARNER,
    )
    organization_id = models.CharField(max_length=64, db_index=True)
    organization_name = models.CharField(max_length=255, blank=True, default="")
    invited_by_email = models.EmailField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    team_member_added = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} -> {self.organization_name or self.organization_id} ({self.status})"


class RegisteredApp(models.Model):
    """AI / IT application in an organisation registry."""

    class AppType(models.TextChoices):
        AI = "ai", "AI"
        IT = "it", "IT"

    class Status(models.TextChoices):
        APPROVED = "approved", "Approved"
        PENDING = "pending", "Pending"
        REJECTED = "rejected", "Rejected"

    class DataAllowed(models.TextChoices):
        PUBLIC_ONLY = "public_only", "Public only"
        INTERNAL = "internal", "Internal"
        INTERNAL_CONFIDENTIAL = "internal_confidential", "Internal + Confidential"
        NONE = "none", "None"

    organization_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=255)
    app_type = models.CharField(max_length=16, choices=AppType.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    category = models.CharField(max_length=100, blank=True, default="")
    data_allowed = models.CharField(
        max_length=32,
        choices=DataAllowed.choices,
        blank=True,
        default="",
    )
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PolicyVersion(models.Model):
    """One uploaded version of an organisation's Acceptable Use Policy PDF.

    The file itself is stored as base64 in Mongo (not on local disk) so it
    survives Render's ephemeral filesystem across deploys/restarts with no
    extra storage service to configure."""

    organization_id = models.CharField(max_length=64, db_index=True)
    file_name = models.CharField(max_length=255)
    file_content_type = models.CharField(max_length=100, default="application/pdf")
    file_base64 = models.TextField()
    version = models.CharField(max_length=32, blank=True, default="")
    effective_date = models.DateField(null=True, blank=True)
    uploaded_by_email = models.EmailField(blank=True, default="")
    uploaded_by_name = models.CharField(max_length=255, blank=True, default="")
    is_live = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.organization_id} · {self.file_name} ({self.version})"


class CardRequest(models.Model):
    """Per-organisation state of one reference card from the (static) Cards
    catalogue — requested / sent / scheduled — powering the quota + Send
    Cards flow on the Admin Center Cards tab."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        SENT = "sent", "Sent"
        SCHEDULED = "scheduled", "Scheduled"
        FAILED = "failed", "Failed"

    organization_id = models.CharField(max_length=64, db_index=True)
    card_id = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.REQUESTED,
    )
    requested_by_email = models.EmailField(blank=True, default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.organization_id} · {self.card_id} ({self.status})"


class CardCustomRequest(models.Model):
    """A "Request a New Card" submission — not a card in the catalogue yet,
    just a request for the AIDL team to review."""

    organization_id = models.CharField(max_length=64, db_index=True)
    requested_by_email = models.EmailField(blank=True, default="")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=32, default="submitted")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.organization_id} · {self.title}"


class TrafficLightRating(models.Model):
    """Single global like/dislike counter for the Traffic Light Check card's
    feedback widget — one row total, created on first vote."""

    likes = models.IntegerField(default=128)
    dislikes = models.IntegerField(default=6)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.likes} likes / {self.dislikes} dislikes"


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
