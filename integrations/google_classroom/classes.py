"""Steps 2 + 3: a teacher's Google Classroom classes and students, in AIDL.

connect()      after the teacher allowed the Classroom permissions: keep the
               refresh token and fetch everything once.
sync()         fetch again later with the stored refresh token.
choose()       step 3: the teacher adds a class to the AIDL programme (or removes it).
for_teacher()  what the classes page / JSON endpoint shows.

Step 3 rules:
  - Adding a class makes each of its students an AIDL learner of the
    teacher's organization (one seat each), linked to their Google account
    ("google:<google user id>", the same id Google sign-in uses). When the
    student signs in with Google later, they are already in the right school.
  - A student who already has an AIDL account (same Google account, or the
    same email) is linked, not duplicated. Someone in another AIDL
    organization is left there and shown as such.
  - Refresh also adds new students of chosen classes.
  - Removing a class only stops adding new students; learners keep their
    account and progress (an admin can remove people in the Admin Center).
"""

from __future__ import annotations

import re

from django.utils import timezone

from api.microsoft_auth import open_refresh_token, seal_refresh_token
from api.models import AIDLUser, Organization

from . import classroom_api, google_oauth
from .google_oauth import GoogleError, GoogleProfile
from .models import GoogleCourse, GoogleCredential, GoogleStudent

PROVIDER = "google_classroom"

# GoogleStudent.aidl_status
ADDED = "added"
NO_SEAT = "no_seat"
OTHER_ORG = "other_org"
INACTIVE = "inactive"
REMOVED = "removed"  # an admin took them out of the programme (Admin Center)


def aidl_course_id(org_id: str) -> str:
    """The AIDL class's Google id ("" when AIDL hasn't made one). Once it
    exists, it is the programme, like #aidl in Slack: its students are the
    team, and the teacher's other classes stay out of AIDL."""
    from .models import AidlClass

    row = AidlClass.objects.filter(organization_id=org_id).exclude(course_id="").first()
    return row.course_id if row else ""


def _allowed(org_id: str, course_id: str) -> bool:
    aidl = aidl_course_id(org_id)
    return not aidl or course_id == aidl


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
        course, _ = GoogleCourse.objects.update_or_create(organization_id=org_id, course_id=c["id"], defaults={
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
        if course.in_programme and _allowed(org_id, c["id"]):
            enrol_class(course)  # new students of a chosen class join AIDL
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
                                                   "email": (profile.get("emailAddress") or "").lower()[:255],
                                                   "synced_at": now,
                                               })
        keep.append(gid)
    GoogleStudent.objects.filter(organization_id=org_id, course_id=course_id).exclude(google_user_id__in=keep).delete()


# --- step 3: classes in the AIDL programme ---

def choose(teacher: AIDLUser, course_id: str, on: bool) -> dict:
    """Add a class to the AIDL programme (on=True) or take it out. Returns enrol counts."""
    course = GoogleCourse.objects.filter(organization_id=teacher.organization_id, course_id=course_id,
                                         teacher_user_id=str(teacher.pk)).first()
    if course is None:
        raise GoogleError("unknown_class", "That class isn't in your list. Press Refresh and try again.")
    if on and not _allowed(teacher.organization_id, course_id):
        raise GoogleError("not_aidl_class", "Only your AIDL class is in the programme. "
                                            "Ask students to join the AIDL class in Google Classroom.")
    course.in_programme = on
    course.added_to_programme_at = timezone.now() if on else None
    course.save(update_fields=["in_programme", "added_to_programme_at"])
    return enrol_class(course) if on else {}


def enrol_class(course: GoogleCourse) -> dict:
    """Make every student of a chosen class an AIDL learner. Returns {status: count}."""
    org = Organization.objects.filter(pk=course.organization_id, is_active=True).first()
    if org is None:
        raise GoogleError("no_organization", "Your AIDL organization is switched off.")
    counts: dict[str, int] = {}
    for student in GoogleStudent.objects.filter(organization_id=course.organization_id, course_id=course.course_id):
        status, user = _enrol(org, student)
        if (student.aidl_status, student.aidl_user_id) != (status, str(user.pk) if user else ""):
            student.aidl_status, student.aidl_user_id = status, str(user.pk) if user else ""
            student.save(update_fields=["aidl_status", "aidl_user_id"])
        counts[status] = counts.get(status, 0) + 1
    return counts


def _follow_google_name(user: AIDLUser, student: GoogleStudent) -> None:
    """The student changed their name in their Google account: AIDL (and their
    licence card) follow it. Only for the account Google sign-in links."""
    name = (student.full_name or "").strip()
    if not name or name == user.full_name or user.microsoft_id != f"google:{student.google_user_id}":
        return
    first, _, last = name.partition(" ")
    user.first_name, user.last_name, user.full_name = first[:100], last[:100], name[:255]
    user.save(update_fields=["first_name", "last_name", "full_name", "updated_at"])


def _enrol(org: Organization, student: GoogleStudent) -> tuple[str, AIDLUser | None]:
    from .plan_limits import can_add_student as can_join  # the plan's teacher / student rules

    user = AIDLUser.objects.filter(microsoft_id=f"google:{student.google_user_id}").first()
    if user is None and student.email:
        user = AIDLUser.objects.filter(email__iexact=student.email).order_by("created_at").first()

    if user is not None:
        if not user.is_active:
            return INACTIVE, user
        if user.organization_id == str(org.pk):
            _follow_google_name(user, student)
            return (REMOVED if user.slack_left_at else ADDED), user
        if user.organization_id:
            return OTHER_ORG, user
        if not can_join(org, user):
            return NO_SEAT, user
        user.organization_id, user.organization_name = str(org.pk), org.name
        user.enroll_as = AIDLUser.EnrollAs.ORGANIZATION
        user.save(update_fields=["organization_id", "organization_name", "enroll_as", "updated_at"])
        return ADDED, user

    if not can_join(org, None):
        return NO_SEAT, None
    first, _, last = (student.full_name or "").partition(" ")
    user = AIDLUser.objects.create(
        microsoft_id=f"google:{student.google_user_id}",
        provider=PROVIDER,
        email=student.email,
        first_name=first[:100],
        last_name=last[:100],
        full_name=student.full_name[:255],
        enroll_as=AIDLUser.EnrollAs.ORGANIZATION,
        role=AIDLUser.Role.LEARNER,
        organization_id=str(org.pk),
        organization_name=org.name,
        is_active=True,
    )
    return ADDED, user


def summary(counts: dict) -> str:
    """{status: count} → a short sentence for the page."""
    parts = []
    if counts.get(ADDED):
        parts.append(f"{counts[ADDED]} student{'s' if counts[ADDED] != 1 else ''} in AIDL")
    if counts.get(NO_SEAT):
        parts.append(f"{counts[NO_SEAT]} not added: no free seats on your plan")
    if counts.get(OTHER_ORG):
        parts.append(f"{counts[OTHER_ORG]} already in another AIDL organization")
    if counts.get(REMOVED):
        parts.append(f"{counts[REMOVED]} removed from the programme by an admin")
    if counts.get(INACTIVE):
        parts.append(f"{counts[INACTIVE]} switched off in AIDL")
    return ", ".join(parts) or "No students in this class yet"


def for_teacher(user: AIDLUser) -> dict:
    cred = GoogleCredential.objects.filter(user_id=str(user.pk)).first()
    courses = list(GoogleCourse.objects.filter(organization_id=user.organization_id, teacher_user_id=str(user.pk)))
    aidl = aidl_course_id(user.organization_id)
    if aidl:  # only the AIDL class (see aidl_course_id)
        courses = [c for c in courses if c.course_id == aidl]
    students: dict[str, list] = {}
    for s in GoogleStudent.objects.filter(organization_id=user.organization_id,
                                          course_id__in=[c.course_id for c in courses]):
        students.setdefault(s.course_id, []).append({
            "id": s.google_user_id, "name": s.full_name, "email": s.email,
            "aidl_status": s.aidl_status, "aidl_user_id": s.aidl_user_id})
    return {
        "connected": bool(cred and cred.refresh_token),
        "google_email": cred.email if cred else "",
        "synced_at": cred.synced_at if cred else None,  # DRF renders it as ISO text
        "classes_in_programme": sum(1 for c in courses if c.in_programme),
        "classes": [{
            "id": c.course_id, "name": c.name, "section": c.section, "room": c.room, "link": c.link,
            "student_count": c.student_count, "in_programme": c.in_programme,
            "students": students.get(c.course_id, []),
        } for c in courses],
    }


# --- step 3: invite the students of the teacher's other classes to the AIDL class ---

INVITE_MAX = 500  # one press; more than any school class list


def other_classes(user: AIDLUser) -> list[dict]:
    """The teacher's classes other than the AIDL class, with how many of their students
    aren't in the AIDL class yet."""
    aidl = aidl_course_id(user.organization_id)
    if not aidl:
        return []
    in_aidl = set(GoogleStudent.objects.filter(organization_id=user.organization_id, course_id=aidl)
                  .values_list("google_user_id", flat=True))
    out = []
    for c in GoogleCourse.objects.filter(organization_id=user.organization_id, teacher_user_id=str(user.pk)
                                         ).exclude(course_id=aidl).order_by("name"):
        ids = set(GoogleStudent.objects.filter(organization_id=user.organization_id, course_id=c.course_id)
                  .values_list("google_user_id", flat=True))
        out.append({"id": c.course_id, "name": c.name, "section": c.section, "student_count": len(ids),
                    "new": len(ids - in_aidl)})
    return out


def bulk_invite_allowed(org: Organization) -> bool:
    """Inviting many students at once (by email or from other classes) is an Enterprise feature."""
    from api.plans import plan_key

    return plan_key(org) == "enterprise"


def _invite_access(teacher: AIDLUser) -> tuple[Organization, str, str]:
    """(organization, AIDL class id, access token) for inviting, or a GoogleError saying what's missing."""
    org = Organization.objects.filter(pk=teacher.organization_id, is_active=True).first()
    aidl = aidl_course_id(teacher.organization_id)
    if org is None or not aidl:
        raise GoogleError("no_aidl_class", "Your AIDL class isn't set up yet.")
    if not bulk_invite_allowed(org):
        raise GoogleError("enterprise_only", "Inviting many students at once is part of the Enterprise plan. "
                                             "Upgrade your plan to use it.")
    cred = GoogleCredential.objects.filter(user_id=str(teacher.pk)).first()
    refresh = open_refresh_token(cred.refresh_token) if cred else ""
    if not refresh:
        raise GoogleError("reconnect", "Connect Google Classroom first.")
    if google_oauth.ROSTERS_SCOPE not in (cred.scopes or "").split():
        raise GoogleError("reconnect", "AIDL needs permission to invite students. Press “Connect Google Classroom” "
                                       "and allow managing your class rosters, then try again.")
    return org, aidl, google_oauth.refresh_access_token(refresh)


def _invite(org: Organization, aidl: str, token: str, people: list[str]) -> dict:
    """Invite these people (Google ids or emails) to the AIDL class as students: skips anyone
    already in it or already invited, and stops at the plan's free seats. Google still asks
    each student to press Join (only a Workspace admin can add people without an invitation).
    Returns counts: invited, already, no_room, failed."""
    from .plan_limits import student_room

    members = classroom_api.list_students(token, aidl) + classroom_api.list_teachers(token, aidl)
    in_aidl = {m["userId"] for m in members}
    in_aidl |= {((m.get("profile") or {}).get("emailAddress") or "").lower() for m in members} - {""}
    invites = classroom_api.list_invitations(token, aidl)
    pending = {i.get("userId") for i in invites}
    counts = {"invited": 0, "already": 0, "no_room": 0, "failed": 0}
    # Seats left, less the student invitations still open (each may join and take one).
    room = min(student_room(org) - sum(1 for i in invites if i.get("role") == "STUDENT"), INVITE_MAX)
    for person in people:
        if person in in_aidl or person in pending:
            counts["already"] += 1
            continue
        if counts["invited"] >= room:
            counts["no_room"] += 1
            continue
        try:
            classroom_api.create_invitation(token, aidl, person)
            counts["invited"] += 1
        except GoogleError as exc:
            if exc.code == "already_exists":  # Google knows them by email: in the class or invited already
                counts["already"] += 1
            elif exc.code == "reconnect":
                raise
            else:
                counts["failed"] += 1
    return counts


def invite_from_classes(teacher: AIDLUser, course_ids: list[str]) -> dict:
    """Invite every student of these classes (the teacher's own) to the AIDL class, in one go."""
    org, aidl, token = _invite_access(teacher)
    mine = set(GoogleCourse.objects.filter(organization_id=teacher.organization_id, teacher_user_id=str(teacher.pk),
                                           course_id__in=course_ids).exclude(course_id=aidl)
               .values_list("course_id", flat=True))
    if not mine:
        raise GoogleError("no_classes", "Tick at least one of your classes.")
    people = []
    for gid in GoogleStudent.objects.filter(organization_id=teacher.organization_id, course_id__in=mine
                                            ).order_by("full_name").values_list("google_user_id", flat=True):
        if gid not in people:
            people.append(gid)
    return _invite(org, aidl, token, people)


EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def emails_in(text: str) -> list[str]:
    """Every email address in pasted text (one per line, commas, a copied spreadsheet column,
    "Name <email>"...), lower-case, each once, in order."""
    out = []
    for email in EMAIL.findall(text or ""):
        email = email.lower().strip(".")
        if email not in out:
            out.append(email)
    return out


def invite_emails(teacher: AIDLUser, text: str) -> dict:
    """Invite a pasted list of student emails to the AIDL class, in one go."""
    emails = emails_in(text)
    if not emails:
        raise GoogleError("no_emails", "Paste at least one student email address.")
    org, aidl, token = _invite_access(teacher)
    return _invite(org, aidl, token, emails)


def invite_summary(counts: dict) -> str:
    parts = [f"📨 {counts['invited']} student{'s' if counts['invited'] != 1 else ''} invited to the AIDL class. "
             "They each get a Google Classroom invitation and press Join; AIDL adds them as learners."]
    if counts.get("already"):
        parts.append(f"{counts['already']} already in the class or invited.")
    if counts.get("no_room"):
        parts.append(f"⚠️ {counts['no_room']} not invited: no free seats on your plan. Upgrade, then invite again.")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} couldn't be invited (Google refused; e.g. outside your school's domain).")
    return " ".join(parts)
