"""Google Classroom data.

GoogleWorkspace   one school (Google Workspace for Education domain) that set up AIDL.
                  One school = one AIDL organization, like one Slack workspace.
                  A teacher with a personal Gmail account (Classroom works without
                  Workspace too) gets a one-person "school" keyed by their Google id.
GoogleCredential  a teacher's (encrypted) Google refresh token for the Classroom API.
GoogleCourse      a class the teacher teaches in Google Classroom.
GoogleStudent     a student of one of those classes (Google's roster). When the class is
                  in the AIDL programme (step 3), aidl_user_id links the AIDL learner.
"""

from django.db import models


class GoogleWorkspace(models.Model):
    # "domain:school.edu" (Workspace account, from Google's verified "hd" claim)
    # or "user:<google sub>" (personal Gmail account).
    key = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, blank=True, default="")
    organization_id = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.key})"


class GoogleCredential(models.Model):
    user_id = models.CharField(max_length=64, unique=True)  # AIDLUser pk
    google_sub = models.CharField(max_length=64)
    email = models.CharField(max_length=255, blank=True, default="")
    refresh_token = models.TextField(blank=True, default="")  # encrypted (api.microsoft_auth.seal_refresh_token)
    scopes = models.TextField(blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.email or self.google_sub


class GoogleCourse(models.Model):
    organization_id = models.CharField(max_length=64, db_index=True)
    teacher_user_id = models.CharField(max_length=64, db_index=True)  # the AIDL user who fetched it
    course_id = models.CharField(max_length=64)  # Google's id
    name = models.CharField(max_length=255)
    section = models.CharField(max_length=255, blank=True, default="")
    room = models.CharField(max_length=255, blank=True, default="")
    link = models.URLField(max_length=500, blank=True, default="")
    state = models.CharField(max_length=32, blank=True, default="")
    student_count = models.IntegerField(default=0)
    synced_at = models.DateTimeField()
    # Step 3: the teacher chose this class for the AIDL programme.
    in_programme = models.BooleanField(default=False)
    added_to_programme_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("organization_id", "course_id")
        ordering = ["name"]

    def __str__(self):
        return self.name


class GoogleStudent(models.Model):
    organization_id = models.CharField(max_length=64, db_index=True)
    course_id = models.CharField(max_length=64, db_index=True)  # Google's course id
    google_user_id = models.CharField(max_length=64)
    full_name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="")
    synced_at = models.DateTimeField()
    # Step 3 (classes.enrol_class): "", added, no_seat, other_org, inactive
    aidl_status = models.CharField(max_length=16, blank=True, default="")
    aidl_user_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        unique_together = ("organization_id", "course_id", "google_user_id")
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name or self.email
