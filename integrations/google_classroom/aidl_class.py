"""The "AIDL" class: Google Classroom's version of Slack's #aidl channel.

When a school admin signs up or signs in (with the Classroom permissions),
setup() runs once per sign-in:

  1. keep their Google access and fetch their classes (classes.connect)
  2. make sure the "AIDL" class exists in their Google Classroom: reuse the
     one we made before (or one they already have with that name), else create it
  3. post the students' welcome in its Stream (everyone in a class sees the
     Stream, so nothing for teachers goes there), or update the one we posted
     before, so signing in again doesn't flood the class
  4. add the 5 Admin Center steps to the class's Classwork as DRAFT materials,
     under the "🚦 AIDL Admin Center" topic, with a "📋 Setup progress" draft on
     top (the Admin Center summary, like Slack's post_admin_center).
     Drafts are seen only by the class's teachers, never by students. Each one
     opens that step's page (Classroom can't show buttons inside a class).
  5. put the AIDL class in the AIDL programme, so students who join it become
     learners (like joining #aidl in Slack)

After every admin action on the AIDL pages, refresh_later() updates the
announcement and the step titles (✅ done, 👉 next) with the teacher's stored
Google access, so Classroom always shows where the setup is.

Nothing here ever stops a sign-in or an admin action: a failure is saved on
AidlClass.error and shown in the Admin Center.
"""

from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from api.models import AIDLUser, Organization

from . import classes, classroom_api, google_oauth
from .google_oauth import GoogleError, GoogleProfile
from .models import AidlClass, GoogleCourse

logger = logging.getLogger(__name__)

NAME = "AIDL"
TOPIC = "🚦 AIDL Admin Center"
SECTION = "AI Driving Licence"
# (key in admin_center.home()["steps"], title, what the teacher does there)
STEPS = (
    ("policy", "Your AI policy",
     "Approve the AI policy cards your learners agree to before their licence. Add AI apps and IT apps."),
    ("admins", "Admins (optional)",
     "Let other teachers help run AIDL, or skip this step."),
    ("team", "Your team",
     "Choose which Google Classroom classes are in AIDL. Their students become learners."),
    ("approval", "Card approval",
     "Approve the two starter cards: the Traffic Light Check and the Highway Code."),
    ("started", "Start the programme",
     "Start AIDL. Learners then get their cards and, at the end, their AI Driving Licence."),
)
STEP_LABELS = tuple((key, title) for key, title, _ in STEPS)


def _url(name: str, *args) -> str:
    return f"{settings.BACKEND_URL.rstrip('/')}{reverse(name, args=args)}"


def _steps(org: Organization, user: AIDLUser) -> dict:
    from .admin_center import home

    return home(org, user)["steps"]


def _next_step(steps: dict) -> int:
    """1-5, or 0 when all done."""
    return next((n for n, (key, _t, _d) in enumerate(STEPS, 1) if not steps.get(key)), 0)


def setup(user: AIDLUser, org: Organization, profile: GoogleProfile, tokens: dict) -> AidlClass | None:
    """After an admin's sign-in. Returns the AidlClass row (with .error set
    when something failed), or None when they didn't grant the permissions."""
    granted = tokens.get("scope", "")
    access_token = tokens.get("access_token", "")
    if not access_token or not google_oauth.has_setup_scopes(granted):
        return None
    row, _ = AidlClass.objects.get_or_create(organization_id=str(org.pk),
                                             defaults={"teacher_user_id": str(user.pk)})
    if row.course_id and row.teacher_user_id != str(user.pk):
        return row  # another admin's Classroom owns it; only they can post in it
    row.teacher_user_id = str(user.pk)
    try:
        _ensure_course(row, access_token)
        _publish(row, org, user, access_token)
        row.error = ""
    except GoogleError as exc:
        logger.warning("google classroom: AIDL class setup failed for org %s: %s", org.pk, exc.code)
        row.error = f"AIDL couldn't set up the AIDL class in Google Classroom: {exc}"
    row.save()

    # Read their classes (the AIDL class included) and put the AIDL class in the programme.
    if google_oauth.has_classroom_scopes(granted):
        try:
            classes.connect(user, profile, tokens)
            if row.course_id:
                course = GoogleCourse.objects.filter(organization_id=str(org.pk), course_id=row.course_id).first()
                if course is not None and not course.in_programme:
                    course.in_programme, course.added_to_programme_at = True, timezone.now()
                    course.save(update_fields=["in_programme", "added_to_programme_at"])
                    classes.enrol_class(course)
                # Other classes stay out (their learners keep their accounts, like removing a class).
                GoogleCourse.objects.filter(organization_id=str(org.pk), in_programme=True).exclude(
                    course_id=row.course_id).update(in_programme=False, added_to_programme_at=None)
        except GoogleError as exc:
            logger.warning("google classroom: reading classes failed for org %s: %s", org.pk, exc.code)
    return row


def refresh(org: Organization) -> None:
    """Bring the announcement and the step titles up to date (after an admin
    action), with the stored Google access of the teacher who owns the class."""
    from api.microsoft_auth import open_refresh_token

    from .models import GoogleCredential

    row = AidlClass.objects.filter(organization_id=str(org.pk)).exclude(course_id="").first()
    if row is None:
        return
    owner = AIDLUser.objects.filter(pk=row.teacher_user_id, is_active=True).first()
    cred = GoogleCredential.objects.filter(user_id=row.teacher_user_id).first()
    token = open_refresh_token(cred.refresh_token) if cred and cred.refresh_token else ""
    if owner is None or not token:
        return
    try:
        _publish(row, org, owner, google_oauth.refresh_access_token(token))
        row.error = ""
    except GoogleError as exc:
        logger.warning("google classroom: AIDL class refresh failed for org %s: %s", org.pk, exc.code)
        row.error = f"AIDL couldn't update the AIDL class in Google Classroom: {exc}"
    row.save()


def refresh_later(org: Organization) -> None:
    """refresh() after the page has answered (it's a few calls to Google)."""
    if getattr(settings, "GOOGLE_CLASSROOM_SYNC", False):  # tests
        from .learners import sync

        refresh(org)
        sync(org)
        return

    def run():
        try:
            refresh(org)
            from .learners import sync
            sync(org)  # the programme may have just started: publish the cards
        except Exception:  # noqa: BLE001 - never let a background refresh crash
            logger.exception("google classroom: AIDL class refresh crashed")
        finally:
            from django.db import connections

            connections.close_all()

    threading.Thread(target=run, daemon=True).start()


# --- the class ---

def _ensure_course(row: AidlClass, access_token: str) -> None:
    if row.course_id:
        try:
            course = classroom_api.get_course(access_token, row.course_id)
            if course.get("courseState") not in ("ARCHIVED", "DECLINED", "SUSPENDED"):
                _keep(row, course)
                return
        except GoogleError as exc:
            if exc.code != "not_found":
                raise
        # deleted / archived: make a new one, with nothing carried over from the old one
        row.course_id, row.announcement_id, row.step_ids, row.topic_id = "", "", {}, ""
        row.cards_topic_id, row.cards, row.licences, row.notices = "", {}, {}, {}

    # One they already have (e.g. our record was lost): don't make a second, but start it afresh.
    for course in classroom_api.list_courses(access_token):
        if course.get("name") == NAME and course.get("section") == SECTION:
            _keep(row, course)
            _clear_leftovers(course["id"], access_token)
            return

    body = {
        "name": NAME,
        "section": SECTION,
        "descriptionHeading": "AIDL: AI Driving Licence",
        "description": "Your school's AIDL class. Students who join it get their AI policy cards and "
                       "their AI Driving Licence.",
        "ownerId": "me",
        "courseState": "ACTIVE",
    }
    try:
        course = classroom_api.create_course(access_token, body)
    except GoogleError:
        # Teachers Google hasn't verified may only create PROVISIONED classes
        # (they accept it in Classroom); try once more without the state.
        body.pop("courseState")
        course = classroom_api.create_course(access_token, body)
    _keep(row, course)


def _clear_leftovers(course_id: str, access_token: str) -> None:
    """A class AIDL has no record of (e.g. the database was cleared, or the school was
    deleted and signed up again): what AIDL posted there before belongs to the old school
    (its welcome, plan, steps, cards and licences) and would sit beside the new copies.
    Only items AIDL made go; the teacher's own posts stay. Cards and Stream posts carry
    Google's associatedWithDeveloper flag; Classwork materials don't, so those are known by
    AIDL's titles (and Google refuses to let AIDL delete anything it didn't make anyway)."""
    lists = ((classroom_api.list_materials, classroom_api.delete_material, _is_aidl_material),
             (classroom_api.list_coursework, classroom_api.delete_coursework, _made_by_aidl),
             (classroom_api.list_announcements, classroom_api.delete_announcement, _made_by_aidl))
    for list_items, delete, ours in lists:
        try:
            items = [i for i in list_items(access_token, course_id) if ours(i)]
        except GoogleError as exc:
            logger.info("google classroom: old items not read (%s)", exc.code)
            continue
        for item in items:
            try:
                delete(access_token, course_id, item["id"])
            except GoogleError as exc:
                if exc.code != "not_found":
                    logger.info("google classroom: old item %s kept (%s)", item["id"], exc.code)


def _made_by_aidl(item: dict) -> bool:
    return bool(item.get("associatedWithDeveloper"))


def _is_aidl_material(item: dict) -> bool:
    import re

    from .learners import AVATAR_TITLE, LICENCE_TITLE

    title = item.get("title") or ""
    return (title in (WELCOME_TITLE, NO_STUDENTS_TITLE, NO_TEACHERS_TITLE, "👥 Add students", LICENCE_TITLE,
                      AVATAR_TITLE)
            or title.startswith(("📋 Setup progress ·", "🎉 Setup done ·", "⭐ Plan: "))
            or bool(re.match(r"^\S+ Step [1-5] · ", title)))


def _keep(row: AidlClass, course: dict) -> None:
    row.course_id = course["id"]
    row.link = (course.get("alternateLink") or "")[:500]
    row.state = course.get("courseState") or ""


def _publish(row: AidlClass, org: Organization, user: AIDLUser, access_token: str) -> None:
    steps = _steps(org, user)
    _sync_steps(row, steps, access_token)
    _post_welcome(row, org, steps, access_token)


# --- the students' welcome (Classwork, on top of the students' topic) ---
# Not on the Stream: Classroom shows teachers every Stream post, so a students'
# post there crowded the teacher's Stream. In Classwork it sits with the cards.

WELCOME_TITLE = "👋 Welcome to AIDL · start here"


def welcome_text(org: Organization, steps: dict) -> str:
    lines = [f"Welcome to AIDL, your AI Driving Licence from {org.name}.", ""]
    if steps.get("started"):
        lines += ["1. Your cards are below in this topic. Start with 🚦 Traffic Light Check.",
                  "2. Read each card and answer “✅ Got it” or “✅ I agree”, then press Turn in.",
                  "3. When every card is done, your licence appears here in Classwork."]
    else:
        lines.append("Your teacher is still setting things up. Your cards will appear below in this topic.")
    return "\n".join(lines)


def _post_welcome(row: AidlClass, org: Organization, steps: dict, access_token: str,
                  on_top: bool = False) -> None:
    """on_top: post it again so it sits above cards published since (Classwork lists the newest first)."""
    from .learners import _topic

    ids = dict(row.step_ids or {})
    old_posts = [row.announcement_id, ids.pop("welcome", "")]  # the old Admin Center / welcome Stream posts
    for post in filter(None, old_posts):
        try:
            classroom_api.delete_announcement(access_token, row.course_id, post)
        except GoogleError as exc:
            if exc.code != "not_found":
                raise
    row.announcement_id = ""
    row.step_ids = ids
    item = ids.get("welcome_item", "")
    if not _welcome_wanted(row):  # no student yet: no students' topic in Classwork at all
        _take_down_welcome(row, ids, access_token)
        return
    text = welcome_text(org, steps)
    if item:
        try:
            if on_top:
                classroom_api.delete_material(access_token, row.course_id, item)
            else:
                classroom_api.update_material(access_token, row.course_id, item, description=text)
                return
        except GoogleError as exc:
            if exc.code != "not_found":
                raise
    ids["welcome_item"] = classroom_api.create_class_material(
        access_token, row.course_id, WELCOME_TITLE, text, topic_id=_topic(row, access_token)).get("id", "")
    row.step_ids = ids


def _welcome_wanted(row: AidlClass) -> bool:
    """The students' topic (welcome, cards, licences) shows once a student the plan lets in
    has joined, or once cards are out."""
    from .learners import allowed_students

    return bool(row.cards) or bool(allowed_students(row))


def _take_down_welcome(row: AidlClass, ids: dict, access_token: str) -> None:
    for remove, item_id in ((classroom_api.delete_material, ids.get("welcome_item")),
                            (classroom_api.delete_topic, row.cards_topic_id)):
        if item_id:
            try:
                remove(access_token, row.course_id, item_id)
            except GoogleError as exc:
                if exc.code != "not_found":
                    raise
    ids["welcome_item"] = ""
    row.cards_topic_id = ""
    row.step_ids = ids


def sync_welcome(row: AidlClass, org: Organization, user: AIDLUser, access_token: str) -> None:
    """On each sync: put up the students' topic when the first student joins (or take it down
    when there's none any more). Only when that changes, not on every poll."""
    if bool((row.step_ids or {}).get("welcome_item")) != _welcome_wanted(row):
        _post_welcome(row, org, _steps(org, user), access_token)


def welcome_on_top(row: AidlClass, org: Organization, user: AIDLUser, access_token: str) -> None:
    """After new cards are published: the welcome goes back above them."""
    _post_welcome(row, org, _steps(org, user), access_token, on_top=True)


# --- the Admin Center summary (Classwork, teacher-only draft) ---

def summary_title(steps: dict) -> str:
    done = sum(1 for key, _t, _d in STEPS if steps.get(key))
    return f"🎉 Setup done · {done} of 5" if done == len(STEPS) else f"📋 Setup progress · {done} of 5 done"


def summary_text(org: Organization, steps: dict) -> str:
    nxt = _next_step(steps)
    lines = [
        f"Set up AIDL for {org.name} in 5 steps:",
        *(f"{'✅' if steps.get(key) else '👉' if n == nxt else '⬜'} {n}. {title}"
          for n, (key, title, _d) in enumerate(STEPS, 1)),
        "",
    ]
    if nxt:
        lines.append(f"Next: open “👉 Step {nxt} · {STEPS[nxt - 1][1]}” below.")
    else:
        lines.append("All 5 steps are done. Your learners are on their way to their AI Driving Licence.")
    lines += ["", "Only teachers see this (it's a draft)."]
    return "\n".join(lines)


# --- the 5 steps (Classwork, teacher-only drafts) ---

def step_title(n: int, steps: dict) -> str:
    key, title, _d = STEPS[n - 1]
    mark = "✅" if steps.get(key) else "👉" if n == _next_step(steps) else "⬜"
    return f"{mark} Step {n} · {title}"


def _ensure_topic(row: AidlClass, access_token: str) -> str:
    """The Classwork topic the steps sit under. "" when Classroom won't allow it
    (e.g. a sign-in from before AIDL asked for the topics permission): the
    steps then show under "No topic"."""
    try:
        topics = classroom_api.list_topics(access_token, row.course_id)
        found = next((t for t in topics if t.get("topicId") == row.topic_id), None) if row.topic_id else None
        found = found or next((t for t in topics if t.get("name") == TOPIC), None)
        row.topic_id = (found or classroom_api.create_topic(access_token, row.course_id, TOPIC)).get("topicId", "")
    except GoogleError as exc:
        logger.info("google classroom: no AIDL topic (%s)", exc.code)
        row.topic_id = ""
    return row.topic_id


def _sync_steps(row: AidlClass, steps: dict, access_token: str) -> None:
    ids = dict(row.step_ids or {})
    topic_id = _ensure_topic(row, access_token)
    # Classwork lists the newest first: create step 5 first so step 1 is on top.
    for n in range(len(STEPS), 0, -1):
        title = step_title(n, steps)
        material_id = ids.get(str(n), "")
        if material_id:
            try:
                fields = {"title": title, **({"topicId": topic_id} if topic_id else {})}
                classroom_api.update_material(access_token, row.course_id, material_id, **fields)
                continue
            except GoogleError as exc:
                if exc.code != "not_found":
                    raise
        ids[str(n)] = _create_step(row.course_id, n, title, access_token, topic_id)
    org = Organization.objects.filter(pk=row.organization_id).first()
    # The plan, then the summary last, so they sit above the steps.
    _sync_plan_item(row, org, ids, topic_id, access_token)
    fields = {"title": summary_title(steps), "description": summary_text(org, steps),
              **({"topicId": topic_id} if topic_id else {})}
    if ids.get("summary"):
        try:
            classroom_api.update_material(access_token, row.course_id, ids["summary"], **fields)
        except GoogleError as exc:
            if exc.code != "not_found":
                raise
            ids["summary"] = ""
    if not ids.get("summary"):
        ids["summary"] = classroom_api.create_draft_material(access_token, row.course_id, fields["title"],
                                                             fields["description"], topic_id=topic_id).get("id", "")
    _sync_no_students_item(org, row, ids, topic_id, access_token)
    row.step_ids = ids




# --- "don't invite" warnings (Classwork, teacher-only drafts, while the plan has no room) ---
# Google sends an invitation the moment a teacher clicks Invite, and apps can't stop it or
# show a message there; so the teacher is told beforehand, on top of the Admin Center, and
# AIDL cancels such invitations (learners._cancel_invitations).

NO_STUDENTS_TITLE = "⛔ Don't invite students: your plan has no room"
NO_TEACHERS_TITLE = "⛔ Don't invite more teachers: your plan is full"


def no_students_text(org: Organization) -> str:
    from .plan_limits import limits, summary

    label = summary(org)["label"]
    why = (f"Your {label} plan is for teachers only, so students can't take the AI Driving Licence."
           if limits(org)["students"] == 0 else f"Your {label} plan is full: there's no room for another student.")
    return "\n".join([why, "",
                      "Don't invite students to this class (People → Invite students) or share the class code "
                      "with them. Google sends the invitation straight away, but AIDL cancels it at its next "
                      "check, so the student sees “Invitation not found”. You're emailed each time.", "",
                      "Upgrade in “⭐ Plan” below to add students. This item goes away once there's room.", "",
                      "Only teachers see this (it's a draft)."])


def no_teachers_text(org: Organization) -> str:
    from .plan_limits import limits, summary

    label, limit = summary(org)["label"], limits(org)["teachers"]
    return "\n".join([f"Your {label} plan allows {limit} teachers, and this class has {limit}.", "",
                      "Don't invite more teachers to this class (People → Invite teachers). Google sends the "
                      "invitation straight away, but AIDL cancels it at its next check, so they see "
                      "“Invitation not found”. You're emailed each time.", "",
                      "Upgrade in “⭐ Plan” below to add teachers. This item goes away once there's room.", "",
                      "Only teachers see this (it's a draft)."])


def _sync_no_students_item(org: Organization, row: AidlClass, ids: dict, topic_id: str, access_token: str) -> None:
    from .plan_limits import can_add_student, teacher_room

    wanted = org is not None and not can_add_student(org, None)
    _sync_warning(row, ids, "no_students", NO_STUDENTS_TITLE, no_students_text(org) if wanted else "",
                  topic_id, access_token)
    room = teacher_room(org, row, access_token) if org is not None else None
    wanted = room is not None and room <= 0
    _sync_warning(row, ids, "no_teachers", NO_TEACHERS_TITLE, no_teachers_text(org) if wanted else "",
                  topic_id, access_token)


def sync_warnings(row: AidlClass, org: Organization, access_token: str) -> None:
    """On each sync: a teacher joining or leaving (or a student seat freeing up) puts up or
    takes down the "don't invite" items, without waiting for an admin action. Only when
    that changes, not on every poll."""
    from .plan_limits import can_add_student, teacher_room

    ids = dict(row.step_ids or {})
    room = teacher_room(org, row, access_token)
    if (bool(ids.get("no_students")) == (not can_add_student(org, None))
            and bool(ids.get("no_teachers")) == (room is not None and room <= 0)):
        return
    _sync_no_students_item(org, row, ids, row.topic_id, access_token)
    row.step_ids = ids


def _sync_warning(row: AidlClass, ids: dict, key: str, title: str, text: str, topic_id: str,
                  access_token: str, link: str = "") -> None:
    """One teacher-only item with this title while text is set; none when it's "". Looks at
    what's really in Classwork, not only the id we kept: two syncs at once (the poller and an
    admin action) can each have made one, and a lost save forgets the id."""
    try:
        found = [m["id"] for m in classroom_api.list_materials(access_token, row.course_id)
                 if m.get("title") == title]
    except GoogleError as exc:
        logger.info("google classroom: Classwork not read (%s)", exc.code)
        found = [ids[key]] if ids.get(key) else []
    keep = (ids.get(key) if ids.get(key) in found else found[0] if found else "") if text else ""
    for extra in (i for i in found if i != keep):
        try:
            classroom_api.delete_material(access_token, row.course_id, extra)
        except GoogleError as exc:
            if exc.code != "not_found":
                raise
    ids[key] = keep
    if not text:
        return
    if keep:
        try:
            classroom_api.update_material(access_token, row.course_id, keep, description=text)
            return
        except GoogleError as exc:
            if exc.code != "not_found":
                raise
    try:
        made = classroom_api.create_draft_material(access_token, row.course_id, title, text, link=link,
                                                   topic_id=topic_id)
    except GoogleError as exc:
        if not link or exc.code == "classroom_forbidden":
            raise
        # Classroom may refuse a link it can't load (e.g. http://localhost): put it in the text instead.
        made = classroom_api.create_draft_material(access_token, row.course_id, title, f"{text}\n{link}",
                                                   topic_id=topic_id)
    ids[key] = made.get("id", "")


def _create_step(course_id: str, n: int, title: str, access_token: str, topic_id: str = "") -> str:
    url = _url("google-classroom-admin-step", n)
    description = f"{STEPS[n - 1][2]}\n\nOnly teachers see this (it's a draft). Open the link to do this step."
    try:
        created = classroom_api.create_draft_material(access_token, course_id, title, description, link=url,
                                                      topic_id=topic_id)
    except GoogleError as exc:
        if exc.code == "classroom_forbidden":
            raise
        # Classroom may refuse a link it can't load (e.g. http://localhost): put it in the text instead.
        created = classroom_api.create_draft_material(access_token, course_id, title,
                                                      f"{description}\n{url}", topic_id=topic_id)
    return created.get("id", "")


# --- the plan (Classwork, teacher-only draft) → the website's plans page ---

def plan_title(org: Organization) -> str:
    from .plan_limits import summary

    s = summary(org)
    students = ("teachers only" if s["teachers_only"] else
                f"{s['students_used']} students · unlimited" if s["students_limit"] is None else
                f"{s['students_used']}/{s['students_limit']} students")
    teachers = f"{s['teachers_used']}/{s['teachers_limit'] if s['teachers_limit'] is not None else '∞'} teachers"
    title = f"⭐ Plan: {s['label']} · {teachers} · {students}"
    pending = _pending_label(org)
    return f"{title} · ⏳ {pending} requested" if pending else title


def _pending_label(org: Organization) -> str:
    """The plan an admin asked for on the website that isn't active yet (no payment on this
    server: api.billing saves it as a request for the AIDL team), else ""."""
    from api.billing import _load
    from api.plans import PLANS

    pending = (_load(org) or {}).get("pending") or {}
    return PLANS.get(pending.get("plan", ""), {}).get("label", "")


def _plan_state(org: Organization) -> str:
    from api.plans import plan_key

    return f"{plan_key(org)}|{_pending_label(org)}"


def plan_text(org: Organization) -> str:
    from api.plans import plan_key

    from .plan_limits import LIMITS

    def who(key):
        lim = LIMITS[key]
        t = "unlimited teachers" if lim["teachers"] is None else f"{lim['teachers']} teachers"
        s = ("unlimited students" if lim["students"] is None else "teachers only, no students" if lim["students"] == 0
             else f"{lim['students']} students")
        return f"{t}, {s}"

    current = plan_key(org)
    lines = ["Plans:"] + [f"{'👉 ' if key == current else '• '}{label}: {who(key)}"
                          for key, label in (("trial", "Trial (free)"), ("basic", "Basic"),
                                             ("enterprise", "Enterprise"))]
    from .learners import removed_students

    row = AidlClass.objects.filter(organization_id=str(org.pk)).first()
    gone = removed_students(row) if row else []
    if gone:
        lines += ["", "⛔ Taken out of the AIDL class because your plan has no room for them:"]
        lines += [f"• {g.get('name') or g.get('email') or 'A ' + g.get('role', 'student')}"
                  f"{' (' + g['email'] + ')' if g.get('email') and g.get('name') else ''}"
                  f"{(' · ' + g.get('role', 'student') + ' invitation cancelled') if g.get('invite') else ''}"
                  f" · {g['removed'][:10]}" for g in gone]
        lines.append("Upgrade your plan, then add them to the class again.")
    pending = _pending_label(org)
    if pending:
        lines += ["", f"⏳ You asked for {pending}. It starts as soon as the AIDL team confirms it "
                      "(or payment goes through); this item then shows your new plan."]
    lines += ["", "Open the link to see the plans and upgrade. When you're done, close that tab: "
                  "Google Classroom updates by itself.", "", "Only teachers see this (it's a draft)."]
    return "\n".join(lines)


def _sync_plan_item(row: AidlClass, org: Organization, ids: dict, topic_id: str, access_token: str) -> None:
    from api.plans import plan_key

    fields = {"title": plan_title(org), "description": plan_text(org),
              **({"topicId": topic_id} if topic_id else {})}
    if ids.get("plan"):
        try:
            classroom_api.update_material(access_token, row.course_id, ids["plan"], **fields)
            ids["plan_key"] = _plan_state(org)
            return
        except GoogleError as exc:
            if exc.code != "not_found":
                raise
    url = _url("google-classroom-admin-upgrade")
    try:
        made = classroom_api.create_draft_material(access_token, row.course_id, fields["title"],
                                                   fields["description"], link=url, topic_id=topic_id)
    except GoogleError as exc:
        if exc.code == "classroom_forbidden":
            raise
        made = classroom_api.create_draft_material(access_token, row.course_id, fields["title"],
                                                   f"{fields['description']}\n{url}", topic_id=topic_id)
    ids["plan"], ids["plan_key"] = made.get("id", ""), _plan_state(org)


def plan_changed(row: AidlClass, org: Organization) -> bool:
    """The plan changed on the website (an upgrade, or a request for one) since Classroom last showed it."""
    return bool((row.step_ids or {}).get("plan")) and (row.step_ids or {}).get("plan_key") != _plan_state(org)


def step_url(n: int) -> str:
    """Where Classwork's "Step n" sends the teacher."""
    return {1: reverse("google-classroom-admin-policy"),
            3: reverse("google-classroom-classes")}.get(n, reverse("google-classroom-admin"))


def info(org: Organization) -> dict:
    """For the Admin Center banner and the "Back to Google Classroom" button."""
    row = AidlClass.objects.filter(organization_id=str(org.pk)).first()
    return {
        "link": row.link if row else "",
        "provisioned": bool(row and row.state == "PROVISIONED"),
        "error": row.error if row else "",
    }
