"""Step 2: a teacher's Google Classroom classes and students, copied into AIDL.

connect()  after the teacher allowed the Classroom permissions: keep the
           refresh token and fetch everything once.
sync()     fetch again later with the stored refresh token.
for_teacher()  what the classes page / JSON endpoint shows.

Students are only Google's roster here; turning them into AIDL learners
(seats, cards, licences) is a later step.
"""

from __future__ import annotations

from django.utils import timezone

from api.microsoft_auth import open_refresh_token, seal_refresh_token
from api.models import AIDLUser

from . import classroom_api, google_oauth
from .google_oauth import GoogleError, GoogleProfile
from .models import GoogleCourse, GoogleCredential, GoogleStudent


def connect(user: AIDLUser, profile: GoogleProfile, tokens: dict) -> int:
    """Save the teacher's Google access, then fetch their classes. Returns the number of classes."""
    if not google_oauth.has_classroom_scopes(tokens.get("scope", "")):
        raise GoogleError("missing_permissions",
                          "AIDL needs all the Google Classroom permissions to read your classes. "
                          "Please connect again and tick every box.")
    cred, _ = GoogleCredential.objects.get_or_create(user_id=str(user.pk), defaults={"google_sub": profile.sub})
    cred.google_sub, cred.email, cred.scopes = profile.sub, profile.email, tokens.get("scope", "")
    if tokens.get("refresh_token"):  # Google sometimes omits it; keep the one we have
        cred.refresh_token = seal_refresh_token(tokens["refresh_token"])
    cred.save()
    return _fetch(user, cred, tokens["access_token"])


def is_connected(user: AIDLUser) -> bool:
    return GoogleCredential.objects.filter(user_id=str(user.pk)).exclude(refresh_token="").exists()


def sync(user: AIDLUser) -> int:
    cred = GoogleCredential.objects.filter(user_id=str(user.pk)).first()
    refresh = open_refresh_token(cred.refresh_token) if cred else ""
    if not refresh:
        raise GoogleError("reconnect", "Connect Google Classroom first.")
    return _fetch(user, cred, google_oauth.refresh_access_token(refresh))


def _fetch(user: AIDLUser, cred: GoogleCredential, access_token: str) -> int:
    org_id, now = user.organization_id, timezone.now()
    courses = classroom_api.list_courses(access_token)
    seen = []
    for c in courses:
        students = classroom_api.list_students(access_token, c["id"])
        GoogleCourse.objects.update_or_create(organization_id=org_id, course_id=c["id"], defaults={
            "teacher_user_id": str(user.pk),
            "name": (c.get("name") or "Untitled class")[:255],
            "section": (c.get("section") or "")[:255],
            "room": (c.get("room") or "")[:255],
            "link": (c.get("alternateLink") or "")[:500],
            "state": c.get("courseState") or "",
            "student_count": len(students),
            "synced_at": now,
        })
        _save_students(org_id, c["id"], students, now)
        seen.append(c["id"])

    # Classes this teacher no longer teaches (archived, removed) drop out.
    gone = GoogleCourse.objects.filter(organization_id=org_id, teacher_user_id=str(user.pk)).exclude(course_id__in=seen)
    gone_ids = list(gone.values_list("course_id", flat=True))
    GoogleStudent.objects.filter(organization_id=org_id, course_id__in=gone_ids).delete()
    gone.delete()

    cred.synced_at = now
    cred.save(update_fields=["synced_at", "updated_at"])
    return len(seen)


def _save_students(org_id: str, course_id: str, students: list[dict], now) -> None:
    keep = []
    for s in students:
        profile = s.get("profile") or {}
        gid = s.get("userId") or profile.get("id") or ""
        if not gid:
            continue
        GoogleStudent.objects.update_or_create(organization_id=org_id, course_id=course_id, google_user_id=gid,
                                               defaults={
                                                   "full_name": ((profile.get("name") or {}).get("fullName")
                                                                 or "")[:255],
                                                   "email": (profile.get("emailAddress") or "")[:255],
                                                   "synced_at": now,
                                               })
        keep.append(gid)
    GoogleStudent.objects.filter(organization_id=org_id, course_id=course_id).exclude(google_user_id__in=keep).delete()


def for_teacher(user: AIDLUser) -> dict:
    cred = GoogleCredential.objects.filter(user_id=str(user.pk)).first()
    courses = list(GoogleCourse.objects.filter(organization_id=user.organization_id, teacher_user_id=str(user.pk)))
    students: dict[str, list] = {}
    for s in GoogleStudent.objects.filter(organization_id=user.organization_id,
                                          course_id__in=[c.course_id for c in courses]):
        students.setdefault(s.course_id, []).append({"id": s.google_user_id, "name": s.full_name, "email": s.email})
    return {
        "connected": bool(cred and cred.refresh_token),
        "google_email": cred.email if cred else "",
        "synced_at": cred.synced_at if cred else None,  # DRF renders it as ISO text
        "classes": [{
            "id": c.course_id, "name": c.name, "section": c.section, "room": c.room, "link": c.link,
            "student_count": c.student_count, "students": students.get(c.course_id, []),
        } for c in courses],
    }
