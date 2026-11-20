"""Students, inside Google Classroom (like Slack's learner DMs).

Students join the AIDL class (People → Invite students, or the class code).
sync(org) then, with the teacher's stored Google access:

  1. reads the AIDL class's students: each becomes an AIDL learner (one seat
     each), like joining #aidl in Slack (classes.sync → enrol_class)
  2. once the admin has started the programme, publishes the cards as
     Classwork questions under the "🎓 Your AI Driving Licence" topic, in the
     Slack order: 🚦 Traffic Light Check, 📖 The Highway Code, then each policy
     card that is due. Students answer inside Classroom ("✅ Got it" /
     "✅ I agree") and turn it in.
  3. reads the answers and records them with the same rules as every other
     platform (api.licensing.acknowledge, the stage order of
     api.employee_flow.stage); an answer given early counts once the student
     reaches that card
  4. issues the licence when every card is done (api.licensing) and posts it
     as a Classwork item that only that student (and the teachers) see.

Google Classroom doesn't tell apps when a student answers, so sync runs:
after admin actions (aidl_class.refresh_later), on the Classes page's
Refresh, from a background poller while the server runs (apps.py,
GOOGLE_CLASSROOM_POLL_SECONDS) and from `manage.py google_classroom_sync`.
"""

from __future__ import annotations

import logging

from api import admin_setup, aup_store, card_approval, card_image
from api.models import AIDLUser, Organization

from . import classes, classroom_api
from .google_oauth import GoogleError
from .models import AidlClass

logger = logging.getLogger(__name__)

TOPIC = "🎓 Your AI Driving Licence"
GOT_IT = "✅ Got it"
AGREE = "✅ I agree"


def _cards(org: Organization) -> list[dict]:
    """The cards a learner answers, in order. key = the acknowledgement item."""
    from api.policy_cards import ICONS, SHORT

    out = [
        {"key": "traffic_light", "version": "1", "answer": GOT_IT,
         "title": "🚦 Traffic Light Check",
         "text": "Before you paste anything into an AI, check the lights. Read the card, then answer "
                 f"“{GOT_IT}” and turn it in.",
         "image": card_image.image_url("traffic_light", org)},
        {"key": "highway_code", "version": "1", "answer": GOT_IT,
         "title": "📖 The Highway Code",
         "text": "The everyday rules for using AI safely and confidently. Read the card, then answer "
                 f"“{GOT_IT}” and turn it in.",
         "image": card_image.image_url("highway_code", org)},
    ]
    for n, version in sorted(aup_store.required_points(org).items()):
        point = aup_store.point(org, n) or {"clauses": []}
        rules = "\n".join(f"• {c['text']}" for c in point["clauses"])
        out.append({"key": aup_store.point_item(n), "version": version, "answer": AGREE,
                    # Short, so Classroom's list shows the topic (the school's name cut it off).
                    "title": f"{ICONS.get(n, '📌')} AI policy · {SHORT.get(n, '')}",
                    "text": f"{rules}\n\nRead the rules, then answer “{AGREE}” and turn it in. "
                            "Your agreement is time-stamped and saved to your AIDL record.",
                    "image": card_image.image_url(f"aup-{n}", org)})
    return out


def _ready(org: Organization) -> bool:
    return admin_setup.started(org) and all(card_approval.is_approved(org, i) for i in card_approval.ONBOARDING)


def sync(org: Organization) -> dict:
    """Run steps 1-4 above. Returns counts for logs / the command."""
    from api.microsoft_auth import open_refresh_token

    from . import google_oauth
    from .models import GoogleCredential

    row = AidlClass.objects.filter(organization_id=str(org.pk)).exclude(course_id="").first()
    if row is None:
        return {}
    owner = AIDLUser.objects.filter(pk=row.teacher_user_id, is_active=True).first()
    cred = GoogleCredential.objects.filter(user_id=row.teacher_user_id).first()
    refresh = open_refresh_token(cred.refresh_token) if cred and cred.refresh_token else ""
    if owner is None or not refresh:
        return {}
    counts = {"cards_published": 0, "answers": 0, "licences": 0}
    from .plan_limits import unlimited_seats

    unlimited_seats(org)  # Enterprise: no student limit, also for the shared licence code
    try:
        token = google_oauth.refresh_access_token(refresh)
        from .aidl_class import plan_changed, refresh

        if plan_changed(row, org):  # upgraded on the website: the plan item, summary and limits
            refresh(org)
            row.refresh_from_db()
        classes.sync(owner)  # new students of the AIDL class become learners
        can_remove = google_oauth.ROSTERS_SCOPE in (cred.scopes or "").split()
        _refuse(row, org, token, can_remove=can_remove)
        if can_remove:
            _cancel_invitations(row, org, token)
        from .aidl_class import sync_warnings

        sync_warnings(row, org, token)  # "don't invite" items follow who joined
        from .aidl_class import sync_welcome, welcome_on_top

        if _ready(org):
            counts["cards_published"] = _publish_cards(row, org, token)
        if counts["cards_published"]:
            welcome_on_top(row, org, owner, token)
        else:
            sync_welcome(row, org, owner, token)  # the students' topic appears with the first student
        if _ready(org):
            counts["answers"], counts["licences"] = _read_answers(
                row, org, token, can_drive=google_oauth.DRIVE_SCOPE in (cred.scopes or "").split())
    except GoogleError as exc:
        logger.warning("google classroom: learner sync failed for org %s: %s", org.pk, exc.code)
    row.save()
    return counts


# --- cards → Classwork questions ---

def _topic(row: AidlClass, token: str) -> str:
    try:
        topics = classroom_api.list_topics(token, row.course_id)
        found = next((t for t in topics if t.get("topicId") == row.cards_topic_id), None) if row.cards_topic_id \
            else None
        found = found or next((t for t in topics if t.get("name") == TOPIC), None)
        row.cards_topic_id = (found or classroom_api.create_topic(token, row.course_id, TOPIC)).get("topicId", "")
    except GoogleError as exc:
        logger.info("google classroom: no learner topic (%s)", exc.code)
        row.cards_topic_id = ""
    return row.cards_topic_id


def allowed_students(row: AidlClass) -> set[str]:
    """Google ids of the AIDL class's students the plan let in (plan_limits):
    only they get the cards, like only #aidl members get them in Slack."""
    from .classes import ADDED
    from .models import GoogleStudent

    rows = GoogleStudent.objects.filter(organization_id=row.organization_id, course_id=row.course_id,
                                        aidl_status=ADDED).exclude(aidl_user_id="")
    ok = set(AIDLUser.objects.filter(pk__in=[s.aidl_user_id for s in rows], is_active=True,
                                     slack_left_at__isnull=True).values_list("pk", flat=True))
    return {s.google_user_id for s in rows if s.aidl_user_id in {str(i) for i in ok}}


REMOVED_SHOWN = 10  # the teacher's list of students AIDL took out keeps the latest ones


def _refuse(row: AidlClass, org: Organization, token: str, can_remove: bool = False) -> None:
    """Students the plan has no room for. Like Slack (which takes them out of #aidl
    again), AIDL takes them out of the AIDL class and lists them for the teacher in
    the "⭐ Plan" item. Without the rosters permission (a sign-in from before AIDL
    asked for it) they stay, see no cards, and get one private post saying why."""
    from django.utils import timezone

    from .classes import NO_SEAT
    from .models import GoogleStudent
    from .plan_limits import refusal

    notes = {gid: ({"post": v} if isinstance(v, str) else v) for gid, v in (row.notices or {}).items()}
    refused = {s.google_user_id: s for s in GoogleStudent.objects.filter(
        organization_id=row.organization_id, course_id=row.course_id, aidl_status=NO_SEAT)}
    removed_now = False
    for gid, student in sorted(refused.items()):
        if can_remove:
            try:
                classroom_api.remove_student(token, row.course_id, gid)
                if notes.get(gid, {}).get("post"):
                    _delete_post(row, token, notes[gid]["post"])
                notes[gid] = {"removed": timezone.now().isoformat(), "name": student.full_name,
                              "email": student.email, "why": refusal(org)}
                student.delete()
                removed_now = True
                continue
            except GoogleError as exc:
                logger.warning("google classroom: couldn't remove student %s (%s)", gid, exc.code)
        if not notes.get(gid, {}).get("post"):
            try:
                notes[gid] = {"post": classroom_api.create_announcement(
                    token, row.course_id, f"⛔ {refusal(org)}", student_ids=[gid]).get("id", "")}
            except GoogleError as exc:
                logger.info("google classroom: plan notice failed (%s)", exc.code)
    for gid in [g for g, n in notes.items() if n.get("post") and g not in refused]:
        _delete_post(row, token, notes.pop(gid)["post"])  # let in since (an upgrade, a free seat)
    removed = sorted((g for g, n in notes.items() if n.get("removed")), key=lambda g: notes[g]["removed"])
    for gid in removed[:-REMOVED_SHOWN]:
        notes.pop(gid)
    row.notices = notes
    if removed_now:  # tell the teacher in the "⭐ Plan" item
        row.step_ids = {**(row.step_ids or {}), "plan_key": ""}


def _cancel_invitations(row: AidlClass, org: Organization, token: str) -> None:
    """Google emails an invitation straight away and won't let apps stop it, so AIDL cancels
    pending invitations to the AIDL class the plan has no room for: the link stops working
    before they can join. The teacher is emailed and sees who in the "⭐ Plan" item.

    Students: while the plan has no room for another student (Basic: none at all). Teachers
    (AIDL admins) invited as students to take the course are kept.
    Teachers: beyond the plan's teachers (Trial: 2), counting the class's teachers and AIDL's
    admins; the first invitations that fit are kept."""
    from django.utils import timezone

    from .plan_limits import can_add_student, refusal, teacher_refusal, teacher_room

    students_full = not can_add_student(org, None)
    room = teacher_room(org, row, token)  # None = no limit
    if not students_full and room is None:
        return
    try:
        invites = classroom_api.list_invitations(token, row.course_id)
    except GoogleError as exc:
        logger.info("google classroom: invitations not read (%s)", exc.code)
        return
    admins = {m.removeprefix("google:") for m in AIDLUser.objects.filter(
        organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True,
        microsoft_id__startswith="google:").values_list("microsoft_id", flat=True)}
    doomed = []
    if students_full:
        doomed += [(i, "student") for i in invites if i.get("role") == "STUDENT" and i.get("userId", "") not in admins]
    if room is not None:
        teachers = [i for i in invites if i.get("role") == "TEACHER"]
        doomed += [(i, "teacher") for i in teachers[max(room, 0):]]
    notes = dict(row.notices or {})
    cancelled = []
    for invite, role in doomed:
        gid = invite.get("userId", "")
        try:
            classroom_api.delete_invitation(token, invite["id"])
        except GoogleError as exc:
            if exc.code != "not_found":
                logger.info("google classroom: invitation %s kept (%s)", invite.get("id"), exc.code)
            continue
        try:
            profile = classroom_api.get_user_profile(token, gid) if gid else {}
        except GoogleError:
            profile = {}
        note = notes[gid or invite["id"]] = {
            "removed": timezone.now().isoformat(), "invite": True, "role": role,
            "name": (profile.get("name") or {}).get("fullName", ""), "email": profile.get("emailAddress", ""),
            "why": teacher_refusal(org) if role == "teacher" else refusal(org)}
        cancelled.append(note)
    if cancelled:
        _email_teacher(row, org, cancelled)
        removed = sorted((g for g, n in notes.items() if isinstance(n, dict) and n.get("removed")),
                         key=lambda g: notes[g]["removed"])
        for gid in removed[:-REMOVED_SHOWN]:
            notes.pop(gid)
        row.notices = notes
        row.step_ids = {**(row.step_ids or {}), "plan_key": ""}  # tell the teacher in the "⭐ Plan" item


def _email_teacher(row: AidlClass, org: Organization, cancelled: list[dict]) -> None:
    """Classroom doesn't say which teacher sent an invitation: the AIDL class's teacher is told."""
    from django.conf import settings
    from django.core.mail import send_mail

    from .plan_limits import limits, summary

    teacher = AIDLUser.objects.filter(pk=row.teacher_user_id).first()
    if teacher is None or not teacher.email:
        return
    label = summary(org)["label"]
    lim = limits(org)
    reasons = {"student": (f"your AIDL {label} plan is for teachers only" if lim["students"] == 0
                           else f"your AIDL {label} plan has no room for another student"),
               "teacher": f"your AIDL {label} plan allows {lim['teachers']} teachers"}
    first = (teacher.first_name or teacher.full_name or "there").split()[0]
    parts = []
    for role in ("teacher", "student"):
        mine = [n for n in cancelled if n.get("role", "student") == role]
        if not mine:
            continue
        who = "\n".join(f"• {n.get('name') or n.get('email') or ('A ' + role)}"
                        f"{' (' + n['email'] + ')' if n.get('email') and n.get('name') else ''}" for n in mine)
        parts.append(f"{'This ' + role + ' invitation' if len(mine) == 1 else 'These ' + role + ' invitations'}, "
                     f"because {reasons[role]}:\n{who}")
    roles = {n.get("role", "student") for n in cancelled}
    add = "teachers" if roles == {"teacher"} else "students" if roles == {"student"} else "people"
    body = (f"Hi {first},\n\nAIDL cancelled invitations to your AIDL class in Google Classroom.\n\n"
            + "\n\n".join(parts) + "\n\n"
            "Google had already emailed the invitation, so they may see “Invitation not found” if they try to join.\n\n"
            f"To add {add}, upgrade your plan from “⭐ Plan” in Classwork → “🚦 AIDL Admin Center”, "
            "then invite them again.\n\nAIDL")
    one = cancelled[0]
    subject = (f"AIDL: {one.get('role', 'student')} invitation cancelled for "
               f"{one.get('name') or one.get('email') or 'a ' + one.get('role', 'student')}"
               if len(cancelled) == 1 else f"AIDL: {len(cancelled)} invitations cancelled")
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [teacher.email], fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - the email is a courtesy; the cancel stands
        logger.warning("google classroom: invitation email to the teacher failed: %s", exc)


def _delete_post(row: AidlClass, token: str, announcement_id: str) -> None:
    try:
        classroom_api.delete_announcement(token, row.course_id, announcement_id)
    except GoogleError:
        pass


def removed_students(row: AidlClass) -> list[dict]:
    """For the teacher: who AIDL took out of the AIDL class, newest first."""
    rows = [n for n in (row.notices or {}).values() if isinstance(n, dict) and n.get("removed")]
    return sorted(rows, key=lambda n: n["removed"], reverse=True)


def _publish_cards(row: AidlClass, org: Organization, token: str) -> int:
    published = dict(row.cards or {})
    allowed = allowed_students(row)
    if not published and not allowed:
        return 0  # no student yet: no students' topic either
    # The topic may have been deleted in Classroom: make it again and put the cards back under it.
    topic_id = _topic(row, token)
    if published:  # a teacher may have deleted a card in Classroom: publish it again
        live = {w["id"]: w for w in classroom_api.list_coursework(token, row.course_id)}
        published = {k: v for k, v in published.items() if v.get("id") in live}
        titles = {c["key"]: c["title"] for c in _cards(org)}
        for key, v in published.items():
            if titles.get(key) and live[v["id"]].get("title") != titles[key]:  # renamed: answers stay
                try:
                    classroom_api.update_coursework(token, row.course_id, v["id"], title=titles[key])
                except GoogleError as exc:
                    logger.info("google classroom: card title not updated (%s)", exc.code)
            if topic_id and live[v["id"]].get("topicId") != topic_id:
                try:
                    classroom_api.update_coursework(token, row.course_id, v["id"], topicId=topic_id)
                except GoogleError as exc:
                    logger.info("google classroom: card topic not set (%s)", exc.code)
            _assign(row, live[v["id"]], allowed, token)
    wanted = _cards(org)
    todo = [c for c in wanted if (published.get(c["key"]) or {}).get("version") != c["version"]]
    row.cards = published
    if not todo or not allowed:  # no student the plan lets in yet: nothing to publish
        return 0
    # Classwork lists the newest first: publish the last card first, so the first card is on top.
    for card in reversed(todo):
        old = published.get(card["key"])
        if old and old.get("id"):  # the card changed: the new version replaces the old question
            try:
                classroom_api.delete_coursework(token, row.course_id, old["id"])
            except GoogleError as exc:
                if exc.code != "not_found":
                    logger.info("google classroom: old card %s kept (%s)", card["key"], exc.code)
        published[card["key"]] = {"id": _create_question(row.course_id, card, topic_id, token, allowed),
                                  "version": card["version"]}
    row.cards = published
    return len(todo)


def _assign(row: AidlClass, work: dict, allowed: set[str], token: str) -> None:
    """Keep a card's students in step with the plan (new students in, removed ones out)."""
    if work.get("assigneeMode") == "INDIVIDUAL_STUDENTS":
        now = set((work.get("individualStudentsOptions") or {}).get("studentIds") or [])
    else:
        now = None  # every student (cards published before the plan rules)
    add = sorted(allowed - (now or set())) if now is not None else sorted(allowed)
    remove = sorted((now or set()) - allowed)
    if now is not None and not add and not remove:
        return
    if not allowed and not now:
        return  # can't give a question to nobody; it stays as it is until someone is allowed
    try:
        if add or now is None:
            classroom_api.modify_assignees(token, row.course_id, work["id"], add=add)
        if remove and len(remove) < len(now or ()):  # Google needs at least one student left
            classroom_api.modify_assignees(token, row.course_id, work["id"], remove=remove)
    except GoogleError as exc:
        logger.info("google classroom: card students not updated (%s)", exc.code)


def _create_question(course_id: str, card: dict, topic_id: str, token: str, allowed: set[str]) -> str:
    body = {
        "title": card["title"],
        "description": card["text"],
        "workType": "MULTIPLE_CHOICE_QUESTION",
        "multipleChoiceQuestion": {"choices": [card["answer"]]},
        "state": "PUBLISHED",
        # Only the students the plan let in see it (plan_limits).
        "assigneeMode": "INDIVIDUAL_STUDENTS",
        "individualStudentsOptions": {"studentIds": sorted(allowed)},
    }
    if topic_id:
        body["topicId"] = topic_id
    try:
        return classroom_api.create_coursework(token, course_id, {**body, "materials": [
            {"link": {"url": card["image"]}}]}).get("id", "")
    except GoogleError as exc:
        if exc.code == "classroom_forbidden":
            raise
        # Classroom may refuse a link it can't load (e.g. http://localhost): no picture then.
        return classroom_api.create_coursework(token, course_id, body).get("id", "")


# --- answers → acknowledgements → licence ---

def _answered(row: AidlClass, token: str) -> dict[str, set[str]]:
    """{card key: Google user ids who turned in an answer}."""
    out: dict[str, set[str]] = {}
    for key, cw in (row.cards or {}).items():
        if not cw.get("id"):
            continue
        try:
            subs = classroom_api.list_submissions(token, row.course_id, cw["id"])
        except GoogleError as exc:
            if exc.code == "not_found":  # a teacher deleted it: published again next time
                row.cards = {k: v for k, v in row.cards.items() if k != key}
                continue
            raise
        out[key] = {s["userId"] for s in subs if s.get("state") in ("TURNED_IN", "RETURNED")
                    and (s.get("multipleChoiceSubmission") or {}).get("answer")}
    return out


def _read_answers(row: AidlClass, org: Organization, token: str, can_drive: bool = False) -> tuple[int, int]:
    from django.utils import timezone

    from api.employee_flow import stage
    from api.licensing import acknowledge, acknowledged_items, issue_licence_if_ready

    answered = _answered(row, token)
    gids = set().union(*answered.values()) if answered else set()
    versions = {key: cw.get("version", "1") for key, cw in (row.cards or {}).items()}
    acks = licences = 0
    posted = dict(row.licences or {})
    for gid in gids:
        # Only Google's students answer questions, so this is someone who joined the AIDL class
        # as a student. They get their cards and licence even if they're also an AIDL admin.
        user = AIDLUser.objects.filter(microsoft_id=f"google:{gid}", organization_id=str(org.pk),
                                       is_active=True).first()
        if user is None or user.slack_left_at:
            continue
        if not user.slack_onboarded_at:
            user.slack_onboarded_at = timezone.now()
            user.save(update_fields=["slack_onboarded_at", "updated_at"])
        done = acknowledged_items(user)
        # The Slack order: Traffic Light, then Highway Code, then the policy cards.
        for key in ("traffic_light", "highway_code"):
            if gid in answered.get(key, ()) and key not in done and stage(user, org) == key:
                acknowledge(user, key)
                acks += 1
        current = aup_store.required_points(org)
        for n in aup_store.pending_points(user, org):
            key = aup_store.point_item(n)
            if gid in answered.get(key, ()) and versions.get(key) == current.get(n):
                acknowledge(user, key, current[n])
                acks += 1
        if stage(user, org) == "licence" and not user.licence_issued:
            if issue_licence_if_ready(user, org) == "issued":
                licences += 1
            user.refresh_from_db()
        entry = posted.get(str(user.pk))
        if isinstance(entry, str):  # posted before the licence picture existed
            entry = {"id": entry, "image": False}
        if entry and entry.get("image") and "name" not in entry:  # posted before names were kept
            entry = posted[str(user.pk)] = {**entry, "name": user.full_name}
        if entry and entry.get("image") and "avatar" not in entry:  # posted before avatars were kept
            entry = posted[str(user.pk)] = {**entry, "avatar": avatar_version(user)}
        # A new name or a new avatar: the licence card is drawn again.
        changed = bool(entry and entry.get("image") and (entry.get("name") != user.full_name
                                                         or entry.get("avatar") != avatar_version(user)))
        # A licence posted on the Stream (before it moved to Classwork) is posted again as a Classwork item:
        # Classroom shows teachers every Stream post, which crowded the teacher's Stream.
        moved = bool(entry and entry.get("kind") != "material")
        if user.licence_issued and (entry is None or moved
                                    or (can_drive and (not entry.get("image") or changed))):
            new = _post_licence(row, org, user, gid, token, can_drive, replacing=entry is not None)
            if new:  # else tried again next sync
                if entry and entry.get("id"):  # the old post gives way (Stream post, no picture yet, old name or avatar)
                    _remove_licence(row, entry, token)
                if entry and entry.get("avatar_ask"):
                    new["avatar_ask"] = entry["avatar_ask"]
                entry = posted[str(user.pk)] = new
        if entry and entry.get("image"):
            _avatar_item(row, user, gid, entry, token)
    row.licences = posted
    return acks, licences


def avatar_version(user: AIDLUser) -> str:
    from api.licence_image import avatar_version as version

    return version(user)


AVATAR_TITLE = "🧑 Create your licence avatar"


def _avatar_item(row: AidlClass, user: AIDLUser, gid: str, entry: dict, token: str) -> None:
    """Like Slack's "Create my avatar": a Classwork item only this student sees,
    opening AIDL's avatar builder. Once they save an avatar, the licence card
    is posted again with it (above) and the item goes away."""
    from api.avatar import start_url

    if not user.avatar_data and not entry.get("avatar_ask"):
        try:
            made = classroom_api.create_student_material(
                token, row.course_id, AVATAR_TITLE,
                "Put your own avatar on your AI Driving Licence. Open the link, build your avatar and press Save, "
                "then close that tab. Your licence card here is updated with it.",
                link=start_url(user), student_ids=[gid], topic_id=row.cards_topic_id)
            entry["avatar_ask"] = made.get("id", "")
        except GoogleError as exc:
            logger.warning("google classroom: avatar item failed for %s: %s", user.pk, exc.code)
    elif user.avatar_data and entry.get("avatar_ask"):
        try:
            classroom_api.delete_material(token, row.course_id, entry["avatar_ask"])
        except GoogleError as exc:
            if exc.code != "not_found":
                logger.info("google classroom: avatar item kept (%s)", exc.code)
                return
        entry.pop("avatar_ask")


def licence_text(org: Organization, user: AIDLUser) -> str:
    first = (user.first_name or user.full_name or "there").split()[0]
    lines = [f"🎉 Well done, {first}! You've earned your AI Driving Licence from {org.name}.",
             "", f"Licence number: {user.licence_number}"]
    if user.licence_expires_at:
        lines.append(f"Valid until: {user.licence_expires_at:%d %b %Y}")
    lines += ["", "Only you and your teachers can see this."]
    return "\n".join(lines)


LICENCE_TITLE = "🪪 Your AI Driving Licence"


def _remove_licence(row: AidlClass, entry: dict, token: str) -> None:
    try:
        if entry.get("kind") == "material":
            classroom_api.delete_material(token, row.course_id, entry["id"])
        else:  # posted on the Stream before
            classroom_api.delete_announcement(token, row.course_id, entry["id"])
    except GoogleError:
        pass


def _licence_picture(org: Organization, user: AIDLUser, gid: str, token: str) -> str:
    """The licence card (the same design as the website's sample licence) in the
    teacher's Drive, viewable by the student. Returns the Drive file id."""
    from api import licence_image

    from .models import GoogleStudent

    file_id = classroom_api.upload_png(token, f"AIDL licence {user.licence_number}.png",
                                       licence_image.png_for(user, org))
    email = user.email or (GoogleStudent.objects.filter(google_user_id=gid).values_list("email", flat=True).first()
                           or "")
    if email:
        classroom_api.share_file(token, file_id, email)
    return file_id


def _post_licence(row: AidlClass, org: Organization, user: AIDLUser, gid: str, token: str,
                  can_drive: bool = False, replacing: bool = False) -> dict | None:
    """A Classwork item only this student (and the teachers) see, under the cards topic, so it
    stays off the Stream. {"id": material id, "kind": "material", "image": bool, ...}, or None
    to try again next sync."""
    file_id = ""
    if can_drive:
        try:
            file_id = _licence_picture(org, user, gid, token)
        except GoogleError as exc:
            logger.warning("google classroom: licence picture failed for %s: %s", user.pk, exc.code)
            if replacing:
                return None  # keep the text post they already have
    try:
        made = classroom_api.create_student_material(token, row.course_id, LICENCE_TITLE, licence_text(org, user),
                                                     drive_file_id=file_id, student_ids=[gid],
                                                     topic_id=row.cards_topic_id)
    except GoogleError as exc:
        logger.warning("google classroom: licence post failed for %s: %s", user.pk, exc.code)
        return None
    return {"id": made.get("id", ""), "kind": "material", "image": bool(file_id), "name": user.full_name,
            "avatar": avatar_version(user)} if made.get("id") else None
