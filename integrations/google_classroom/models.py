"""Google Classroom data.

GoogleWorkspace   one school (Google Workspace for Education domain) that set up AIDL.
                  One school = one AIDL organization, like one Slack workspace.
                  A teacher with a personal Gmail account (Classroom works without
                  Workspace too) gets a one-person "school" keyed by their Google id.
GoogleCredential  a teacher's (encrypted) Google refresh token for the Classroom API.
GoogleCourse      a class the teacher teaches in Google Classroom.
AidlClass         the "AIDL" class AIDL creates in the first admin's Google Classroom
                  at sign-up (like Slack's #aidl channel), with its Admin Center post.
GoogleStudent     a student of one of those classes (Google's roster). When the class is
                  in the AIDL programme (step 3), aidl_user_id links the AIDL learner.
SchoolSetup       the admin's short setup after sign-up: approximate student / teacher
                  numbers, and when the school's AI policy was confirmed (onboarding.py).
SchoolDirectory   Enterprise "whole school": the school's Google Workspace admin let AIDL read
                  the school's accounts, so every student is added straight into the AIDL class
                  (school_directory.py).
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


class AidlClass(models.Model):
    organization_id = models.CharField(max_length=64, unique=True)
    teacher_user_id = models.CharField(max_length=64)  # the admin whose Classroom it's in (its owner)
    course_id = models.CharField(max_length=64, blank=True, default="")  # Google's id
    link = models.URLField(max_length=500, blank=True, default="")
    state = models.CharField(max_length=32, blank=True, default="")  # ACTIVE, or PROVISIONED (teacher must accept)
    announcement_id = models.CharField(max_length=64, blank=True, default="")
    step_ids = models.JSONField(default=dict, blank=True)  # {"1": Classwork material id, ...}
    topic_id = models.CharField(max_length=64, blank=True, default="")  # the "AIDL Admin Center" Classwork topic
    # Learners (learners.py): the cards as Classwork questions, and the licence posts.
    cards_topic_id = models.CharField(max_length=64, blank=True, default="")
    cards = models.JSONField(default=dict, blank=True)  # {card key: {"id": courseWork id, "version": ...}}
    licences = models.JSONField(default=dict, blank=True)  # {AIDL user id: {"id": Classwork material id, ...}}
    notices = models.JSONField(default=dict, blank=True)  # {Google user id: post telling them the plan has no room}
    error = models.TextField(blank=True, default="")  # why the last setup failed, shown in the Admin Center
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AIDL class {self.course_id or '(not created)'}"


class SchoolSetup(models.Model):
    """The short setup a school's admin does after signing up (onboarding.py): roughly how
    many students and teachers, then the school's AI policy in one click."""

    organization_id = models.CharField(max_length=64, unique=True)
    # A range key from onboarding.STUDENT_RANGES / TEACHER_RANGES: an estimate, never a limit.
    approx_students = models.CharField(max_length=16, blank=True, default="")
    approx_teachers = models.CharField(max_length=16, blank=True, default="")
    details_by = models.CharField(max_length=64, blank=True, default="")  # AIDL user id
    details_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)  # policy confirmed: programme started
    invited = models.JSONField(default=dict, blank=True)  # what finishing invited automatically (counts / why not)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Setup {self.organization_id}"


class SchoolDirectory(models.Model):
    """One school's whole-school connection, made by its Google Workspace (IT) admin."""

    organization_id = models.CharField(max_length=64, unique=True)
    domain = models.CharField(max_length=255)  # school.edu: only this domain's admin can connect
    admin_user_id = models.CharField(max_length=64)  # the AIDL user (the IT admin) whose access is stored
    admin_email = models.CharField(max_length=255, blank=True, default="")
    refresh_token = models.TextField(blank=True, default="")  # encrypted (api.microsoft_auth.seal_refresh_token)
    scopes = models.TextField(blank=True, default="")
    # The school's organisational units (folders) whose accounts are students, e.g. ["/Students"].
    student_units = models.JSONField(default=list, blank=True)
    student_count = models.IntegerField(default=0)
    counted_at = models.DateTimeField(null=True, blank=True)
    preview = models.JSONField(default=list, blank=True)  # a few student names, shown before sending
    # The admin pressed "Send AIDL to all students": the daily check keeps adding new students.
    send_all = models.BooleanField(default=False)
    status = models.CharField(max_length=16, blank=True, default="")  # "", adding, done, error
    progress = models.JSONField(default=dict, blank=True)  # counts of the last run (school_directory.run)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Whole school {self.domain}"
