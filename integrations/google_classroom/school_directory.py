"""Enterprise "whole school": every student of the school in the AIDL class, without invitations.

A teacher's Google access only reaches their own classes, so the school's Google
Workspace (IT) admin connects AIDL once:

  1. connect()   the IT admin allows the whole-school permissions (google_oauth.SCHOOL_SCOPES).
                 Google answers 403 to non-admins, so a teacher gets "ask your IT admin" and a
                 link to send them (link_token(): signed, 7 days, only for the school's domain).
                 The IT admin becomes an AIDL admin of the school.
  2. count()     the admin ticks which organisational units ("folders", e.g. /Students) hold
                 the students; AIDL counts them (suspended / archived accounts left out).
  3. start()     "Send AIDL to all students": run() adds each student straight into the AIDL
                 class with the IT admin's access (an admin needs no invitation), in the
                 background, saving progress as it goes. If Google refuses (e.g. a delegated
                 admin without Classroom rights), that student gets an invitation instead.
                 From there the usual learner sync (learners.py) makes them learners and sends
                 the cards.
  4. sync_due()  once a day (poller / `manage.py google_classroom_sync`) run() again, so new
                 students join automatically. Nobody is taken out: a student whose account the
                 school suspends or deletes can't open Classroom, and keeps any licence earned.

One AIDL class per school (AidlClass), so a school bigger than Google's class size
limit (CLASS_LIMIT) fills the class and is told how many didn't fit.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

from api.microsoft_auth import open_refresh_token, seal_refresh_token
from api.models import AIDLUser, Organization

from . import classes, classroom_api, directory_api, google_oauth
from .google_oauth import GoogleError, GoogleProfile
from .models import GoogleWorkspace, SchoolDirectory

logger = logging.getLogger(__name__)

ADDING, DONE, ERROR = "adding", "done", "error"
LINK_SALT = "aidl.google_classroom.school"
LINK_DAYS = 7
PREVIEW = 8  # student names shown before sending
SAVE_EVERY = 25  # progress saved while adding


def class_limit() -> int:
    """Google's limit on people in one class. Check Google's current number; override with
    GOOGLE_CLASSROOM_CLASS_LIMIT."""
    return int(os.getenv("GOOGLE_CLASSROOM_CLASS_LIMIT", "1000") or 1000)


def recheck_hours() -> int:
    return int(os.getenv("GOOGLE_CLASSROOM_SCHOOL_SYNC_HOURS", "24") or 24)


def _pause() -> float:
    """Seconds between two adds, to stay inside Google's rate limits."""
    return float(os.getenv("GOOGLE_CLASSROOM_ADD_PAUSE", "0.1") or 0)


# --- who may use it ---

def allowed(org: Organization) -> bool:
    return classes.bulk_invite_allowed(org)  # Enterprise


def school_domain(org: Organization) -> str:
    """The school's Google Workspace domain ("" for a personal-Gmail "school")."""
    ws = GoogleWorkspace.objects.filter(organization_id=str(org.pk)).exclude(domain="").order_by("created_at").first()
    return ws.domain if ws else ""


def get(org: Organization) -> SchoolDirectory | None:
    return SchoolDirectory.objects.filter(organization_id=str(org.pk)).first()


# --- the link a teacher sends to their IT admin ---

def link_token(org: Organization, domain: str) -> str:
    return signing.dumps({"o": str(org.pk), "d": domain}, salt=LINK_SALT, compress=True)


def read_link_token(token: str) -> tuple[Organization, str]:
    """(organization, domain) from a link_token, or GoogleError when it's broken or too old."""
    try:
        data = signing.loads(token or "", salt=LINK_SALT, max_age=timedelta(days=LINK_DAYS))
    except signing.SignatureExpired as exc:
        raise GoogleError("link_expired", "This link has expired. Ask your teacher for a new one.") from exc
    except signing.BadSignature as exc:
        raise GoogleError("link_invalid", "This link isn't valid. Ask your teacher for a new one.") from exc
    org = Organization.objects.filter(pk=data.get("o"), is_active=True).first()
    if org is None or not data.get("d"):
        raise GoogleError("link_invalid", "This school's AIDL account no longer exists.")
    return org, data["d"]


# --- 1. connect ---

def connect(org: Organization, domain: str, profile: GoogleProfile, tokens: dict) -> AIDLUser:
    """After the IT admin allowed the whole-school permissions. Returns their AIDL user."""
    if not allowed(org):
        raise GoogleError("enterprise_only", "Adding the whole school is part of the Enterprise plan.")
    if (profile.hd or "").lower() != domain.lower():
        raise GoogleError("wrong_school", f"Sign in with your school's Google account (@{domain}).")
    if not google_oauth.has_school_scopes(tokens.get("scope", "")):
        raise GoogleError("missing_permissions", "AIDL needs every permission on Google's screen to add your "
                                                 "students. Please connect again and tick every box.")
    flags = directory_api.admin_flags(tokens["access_token"], profile.email)  # 403 → not_admin
    if not (flags.get("isAdmin") or flags.get("isDelegatedAdmin")):
        raise GoogleError("not_admin", "This Google account isn't an admin of your school's Google Workspace. "
                                       "Only your IT admin can connect the whole school.")
    user = _aidl_admin(org, profile)
    row, _ = SchoolDirectory.objects.get_or_create(organization_id=str(org.pk),
                                                   defaults={"domain": domain, "admin_user_id": str(user.pk)})
    row.domain, row.admin_user_id, row.admin_email = domain, str(user.pk), profile.email
    row.scopes = tokens.get("scope", "")
    if tokens.get("refresh_token"):  # Google sometimes omits it; keep the one we have
        row.refresh_token = seal_refresh_token(tokens["refresh_token"])
    if not row.refresh_token:
        raise GoogleError("reconnect", "Google didn't give AIDL lasting access. Please connect again.")
    row.error = ""
    if row.status == ERROR:
        row.status = ""
    row.save()
    logger.info("google classroom: whole school connected for org %s by %s", org.pk, user.pk)
    return user


def _aidl_admin(org: Organization, profile: GoogleProfile) -> AIDLUser:
    """The IT admin as an AIDL admin of this school (made, joined or promoted)."""
    from .accounts import _join, _new_user, find_user

    user = find_user(profile)
    if user is not None and not user.is_active:
        raise GoogleError("account_disabled", "Your AIDL account is switched off.")
    if user is not None and user.organization_id and user.organization_id != str(org.pk):
        raise GoogleError("other_org", "Your Google account already belongs to another AIDL organization.")
    if user is None:
        return _new_user(profile, org, admin=True)
    if not user.organization_id or user.role != AIDLUser.Role.ADMIN:
        _join(user, org, admin=True)
    return user


def disconnect(org: Organization) -> None:
    """Stop: AIDL forgets the IT admin's access. Students already added stay in the class."""
    SchoolDirectory.objects.filter(organization_id=str(org.pk)).delete()


def _token(row: SchoolDirectory) -> str:
    refresh = open_refresh_token(row.refresh_token) if row.refresh_token else ""
    if not refresh:
        raise GoogleError("reconnect", "Your IT admin needs to connect the whole school again.")
    return google_oauth.refresh_access_token(refresh)


# --- 2. which accounts are students ---

def org_units(row: SchoolDirectory, token: str = "") -> list[dict]:
    units = directory_api.list_org_units(token or _token(row))
    return sorted(({"path": u.get("orgUnitPath", ""), "name": u.get("name", ""),
                    "description": u.get("description", "")} for u in units if u.get("orgUnitPath")),
                  key=lambda u: u["path"].lower())


def students(row: SchoolDirectory, token: str) -> list[dict]:
    """Every active account in the chosen units, once each: {"id", "email", "name"}."""
    out: dict[str, dict] = {}
    for path in row.student_units:
        for u in directory_api.list_users_in(token, path):
            if u.get("suspended") or u.get("archived") or not u.get("id") or u["id"] in out:
                continue
            out[u["id"]] = {"id": u["id"], "email": (u.get("primaryEmail") or "").lower(),
                            "name": (u.get("name") or {}).get("fullName") or u.get("primaryEmail") or ""}
    return sorted(out.values(), key=lambda s: s["name"].lower())


def count(org: Organization, paths: list[str]) -> int:
    """Save which units hold the students and count them."""
    row = get(org)
    if row is None:
        raise GoogleError("not_connected", "Your IT admin hasn't connected the whole school yet.")
    token = _token(row)
    known = {u["path"] for u in org_units(row, token)}
    chosen = [p for p in dict.fromkeys(paths) if p in known and p != "/"]
    if not chosen:
        raise GoogleError("no_units", "Tick the folders that hold your students.")
    row.student_units = chosen
    people = students(row, token)
    row.student_count, row.counted_at = len(people), timezone.now()
    row.preview = [p["name"] for p in people[:PREVIEW]]
    row.save()
    return len(people)


# --- 3. add everyone ---

def start(org: Organization) -> None:
    row = get(org)
    if row is None or not row.student_units:
        raise GoogleError("no_units", "Choose which folders hold your students first.")
    if not classes.aidl_course_id(str(org.pk)):
        raise GoogleError("no_aidl_class", "Your AIDL class isn't set up in Google Classroom yet.")
    row.send_all, row.status, row.error = True, ADDING, ""
    row.save()
    run_later(org)


def run(org: Organization) -> dict:
    """Add every chosen student who isn't in the AIDL class yet. Returns the counts."""
    row = get(org)
    if row is None or not row.send_all:
        return {}
    counts = {"total": 0, "in_class": 0, "added": 0, "invited": 0, "failed": 0, "class_full": 0}
    try:
        if not allowed(org):
            raise GoogleError("enterprise_only", "Adding the whole school is part of the Enterprise plan.")
        aidl = classes.aidl_course_id(str(org.pk))
        if not aidl:
            raise GoogleError("no_aidl_class", "Your AIDL class isn't set up in Google Classroom yet.")
        row.status = ADDING
        row.save(update_fields=["status", "updated_at"])
        token = _token(row)
        people = students(row, token)
        members = classroom_api.list_students(token, aidl) + classroom_api.list_teachers(token, aidl)
        in_ids = {m.get("userId") for m in members}
        in_emails = {((m.get("profile") or {}).get("emailAddress") or "").lower() for m in members} - {""}
        missing = [p for p in people if p["id"] not in in_ids and p["email"] not in in_emails]
        counts["total"], counts["in_class"] = len(people), len(people) - len(missing)
        row.student_count = len(people)
        room = max(0, class_limit() - len(members))
        for n, person in enumerate(missing, 1):
            if counts["added"] + counts["invited"] >= room:
                counts["class_full"] += 1
                continue
            _add(token, aidl, person, counts)
            if n % SAVE_EVERY == 0:
                row.progress = dict(counts)
                row.save(update_fields=["progress", "updated_at"])
            if _pause():
                time.sleep(_pause())
        row.status, row.error = DONE, ""
    except GoogleError as exc:
        logger.warning("google classroom: whole school failed for org %s: %s", org.pk, exc.code)
        row.status, row.error = ERROR, str(exc)
    row.progress, row.last_sync_at = counts, timezone.now()
    row.save()
    return counts


def _add(token: str, aidl: str, person: dict, counts: dict) -> None:
    try:
        classroom_api.add_student(token, aidl, person["id"])
        counts["added"] += 1
        return
    except GoogleError as exc:
        if exc.code == "already_exists":
            counts["in_class"] += 1
            return
        if exc.code == "reconnect":
            raise
        if exc.code != "classroom_forbidden":
            counts["failed"] += 1
            return
    try:  # Google didn't let this admin add directly: an invitation still reaches them
        classroom_api.create_invitation(token, aidl, person["id"])
        counts["invited"] += 1
    except GoogleError as exc:
        if exc.code == "already_exists":
            counts["in_class"] += 1
        elif exc.code == "reconnect":
            raise
        else:
            counts["failed"] += 1


def run_later(org: Organization) -> None:
    """run() after the page has answered, then the learner sync (new learners, their cards)."""
    from .learners import sync

    if getattr(settings, "GOOGLE_CLASSROOM_SYNC", False):  # tests
        run(org)
        sync(org)
        return

    def go():
        try:
            run(org)
            sync(org)
        except Exception:  # noqa: BLE001 - never let a background run crash
            logger.exception("google classroom: whole school run crashed")
        finally:
            from django.db import connections

            connections.close_all()

    threading.Thread(target=go, name="google-classroom-school", daemon=True).start()


# --- 4. every day ---

def due(row: SchoolDirectory) -> bool:
    if not row.send_all or (row.status == ADDING and row.updated_at > timezone.now() - timedelta(hours=1)):
        return False  # off, or still running
    return row.last_sync_at is None or row.last_sync_at <= timezone.now() - timedelta(hours=recheck_hours())


def sync_due() -> dict:
    """The schools whose daily check is due (poller.sync_all)."""
    totals: dict[str, int] = {}
    for row in SchoolDirectory.objects.filter(send_all=True):
        org = Organization.objects.filter(pk=row.organization_id, is_active=True).first()
        if org is None or not due(row):
            continue
        for k, v in run(org).items():
            totals[f"school_{k}"] = totals.get(f"school_{k}", 0) + v
    return totals


# --- the page ---

def page(org: Organization) -> dict:
    """What the Whole school page shows."""
    row = get(org)
    ctx = {"enterprise": allowed(org), "domain": school_domain(org), "row": row,
           "aidl_class": bool(classes.aidl_course_id(str(org.pk))), "units": [], "units_error": "",
           "class_limit": class_limit(), "adding": bool(row and row.status == ADDING),
           "over_limit": bool(row and row.student_count > class_limit())}
    if row is not None and ctx["enterprise"]:
        try:
            ctx["units"] = [{**u, "on": u["path"] in row.student_units} for u in org_units(row)]
        except GoogleError as exc:
            ctx["units_error"] = str(exc)
    return ctx
