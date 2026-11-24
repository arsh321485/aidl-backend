"""Google Classroom sign-up / sign-in (step 1) and classes + students (step 2).

Step 1
GET /api/google-classroom/login/?enroll_as=organization|individual
    → {"auth_url": ..., "mode": "google_classroom"}   (frontend sends the browser there)
    Add &redirect=1 to be sent straight to Google (handy for testing in a browser).
GET /api/google-classroom/callback/
    Google sends the browser back here. We sign the person in and redirect to
    the frontend's AUTH_SUCCESS_REDIRECT (/auth/callback) with the same token
    params as Slack / Teams (access_token, refresh_token, ..., mode=google_classroom),
    or with ?error=&error_description=.
    One button does both sign-up and sign-in: a new Google account is created,
    a known one just signs in.
    A school (organization) sign-in also grants the Classroom permissions: for an
    admin AIDL then creates / reuses the "AIDL" class with the Admin Center post
    (aidl_class.py), and the website opens that class in Google Classroom (/open/).
GET /api/google-classroom/open/
    Where the website sends an admin after sign-in: the AIDL class in Google Classroom, whose
    Classwork holds the setup steps (onboarding.py); each opens its page in a new tab, and
    "Back to Google Classroom" closes it. Without an AIDL class: the setup page itself.

Step 2 (AIDL admins = teachers)
GET  /api/google-classroom/classes/           page: the teacher's classes and students
GET  /api/google-classroom/classes/connect/   → Google, asking for the Classroom permissions
POST /api/google-classroom/classes/sync/      fetch again (page button)
POST /api/google-classroom/classes/choose/    step 3: add a class to the AIDL programme (course_id, on=1|0)
POST /api/google-classroom/classes/signout/
GET  /api/google-classroom/api/classes/       same data as JSON (Authorization: Bearer <AIDL access token>)
POST /api/google-classroom/api/classes/sync/  fetch again, JSON
POST /api/google-classroom/api/classes/choose/  {"course_id": "...", "in_programme": true|false}, JSON
POST /api/google-classroom/classes/setup/     step 4: approve the 2 starter cards / start the programme

Step 4 (learners)
GET  /api/google-classroom/learn/             the link a teacher shares in Google Classroom: sign in with
                                              Google, then the learner flow (cards -> licence)
POST /api/google-classroom/learn/action/      a learner's button

Enterprise "whole school" (the IT admin adds every student): school_views.py
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from api.auth_jwt import create_access_token, create_refresh_token
from api.auth_views import JWTAuthentication
from api.microsoft_auth import consume_oauth_state_with_payload, create_oauth_state
from api.models import AIDLUser
from api.org_service import get_organization_for_user
# The learner flow (cards -> licence) is the same for every LMS. It only calls
# api/; it lives in the Moodle folder because Moodle had it first.
from integrations.moodle import flow

from . import aidl_class, classes, google_oauth, session
from .accounts import find_user, sign_in
from .google_oauth import GoogleError

logger = logging.getLogger(__name__)

MODE = "google_classroom"
CLASSES = "google_classroom:classes"  # the OAuth state payload of a step-2 connect
LEARN = "google_classroom:learn"  # ... of a step-4 learner sign-in (the shared link)
SCHOOL = "google_classroom:school"  # ... of an IT admin connecting the whole school ("|<link token>", school_views)


def _enroll_as(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if value in {"individual", "organization"} else "organization"


# --- step 1: sign up / sign in ---

@require_GET
def login(request):
    if not google_oauth.configured():
        return JsonResponse({"error": "google_not_configured",
                             "message": "Google Classroom sign-in isn't set up on the server yet "
                                        "(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)."}, status=503)
    enroll_as = _enroll_as(request.GET.get("enroll_as"))
    state = create_oauth_state(enroll_as, payload=MODE)
    # A school also lets AIDL read its classes and create the AIDL class (like "Add to Slack").
    url = google_oauth.auth_url(state, classroom=enroll_as == "organization")
    if request.GET.get("redirect") == "1":
        return HttpResponseRedirect(url)
    return JsonResponse({"auth_url": url, "state": state, "enroll_as": enroll_as, "mode": MODE})


@require_GET
def callback(request):
    """Google's one redirect URI: website sign-in, connecting classes, learner sign-in."""
    enroll_as, payload, state_error = consume_oauth_state_with_payload(request.GET.get("state", ""))
    if not state_error and (payload or "").startswith(SCHOOL + "|"):  # an IT admin connecting the whole school
        return _school_callback(request, payload.split("|", 1)[1])
    for_classes = payload == CLASSES
    fail = _classes_error if for_classes else _learn_error if payload == LEARN else _error

    if request.GET.get("error"):
        return fail(request, request.GET["error"], "Google sign-in was cancelled.")
    code = request.GET.get("code")
    if not code:
        return fail(request, "code_missing", "Google did not return a sign-in code.")
    if state_error or payload not in (MODE, CLASSES, LEARN):  # unknown, expired, reused, or another platform's state
        return fail(request, state_error or "invalid_state", "Your sign-in session expired. Please try again.")
    if not google_oauth.configured():
        return fail(request, "google_not_configured", "Google Classroom sign-in isn't set up on the server yet.")

    try:
        if for_classes:
            return _connect_classes(request, code)
        if payload == LEARN:
            return _learner_signed_in(request, code)
        tokens = google_oauth.exchange_tokens(code)
        profile = google_oauth.verify_id_token(tokens["id_token"])
        user, org = sign_in(profile, _enroll_as(enroll_as))
    except GoogleError as exc:
        return fail(request, exc.code, str(exc))
    if org is not None and user.role == AIDLUser.Role.ADMIN:
        aidl_class.setup(user, org, profile, tokens)  # never fails the sign-in
    return _redirect(request, user)


def _school_callback(request, token: str):
    from . import school_views

    retry = f"{reverse('google-classroom-school-it-admin')}?t={token}"
    if request.GET.get("error"):
        return school_views._message(request, "Can't connect the whole school", "Google sign-in was cancelled.",
                                     400, retry_url=retry)
    if not request.GET.get("code"):
        return school_views._message(request, "Can't connect the whole school",
                                     "Google did not return a sign-in code.", 400, retry_url=retry)
    return school_views.connected(request, request.GET["code"], token)


def _redirect(request, user: AIDLUser):
    """Back to the website with AIDL tokens. Also signs the person in to our
    own pages (the cookie), so an admin's "Open Admin Center" on the website
    needs no second Google sign-in."""
    query = {
        "access_token": create_access_token(user),
        "refresh_token": create_refresh_token(user),
        "mode": MODE,
        "enroll_as": user.enroll_as,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
    }
    if user.role == AIDLUser.Role.ADMIN and user.organization_id:
        # The website opens this link after sign-in: the AIDL class in Google
        # Classroom, whose post shows the 5 Admin Center steps (see open_classroom).
        open_url = f"{settings.BACKEND_URL.rstrip('/')}{reverse('google-classroom-open')}"
        query["admin_center_url"] = f"{open_url}?{urlencode({'email': user.email})}"
        query["open_admin_center"] = "1"
    response = HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")
    return session.set_session(request, response, user) if user.organization_id else response


@require_GET
def open_classroom(request):
    """The link the website opens after an admin's sign-in: the AIDL class in
    Google Classroom with its Admin Center post (like Slack opening #aidl), or
    Google Classroom's home when there's no AIDL class. authuser picks the
    Google account that just signed in, when the browser has several."""
    from .models import AidlClass

    from .onboarding import next_url

    user = session.signed_in(request)
    row = AidlClass.objects.filter(organization_id=user.organization_id).first() if user and user.organization_id else None
    if not (row and row.link) and user is not None and user.role == AIDLUser.Role.ADMIN \
            and (org := get_organization_for_user(user)) and (setup := next_url(org)):
        # No AIDL class to show the setup steps in (Classroom permissions not granted): the setup itself.
        return session.clear_focus_step(redirect(setup))
    # Otherwise the AIDL class: its Classwork's "👉 Step 1 · School details" starts the setup.
    base = row.link if row and row.link else GOOGLE_CLASSROOM_URL
    email = request.GET.get("email", "").strip()
    if "@" in email:
        base += ("&" if "?" in base else "?") + urlencode({"authuser": email})
    return session.clear_focus_step(HttpResponseRedirect(base))


GOOGLE_CLASSROOM_URL = "https://classroom.google.com/"


def _error(request, code: str, description: str):
    logger.info("google classroom sign-in failed: %s", code)
    query = {"mode": MODE, "error": code, "error_description": description}
    return HttpResponseRedirect(f"{settings.AUTH_SUCCESS_REDIRECT}?{urlencode(query)}")


# --- step 2: classes + students ---

def _teacher_refusal(user: AIDLUser | None) -> str:
    """"" when this person may connect / see Google classes, else why not."""
    if user is None:
        return ("You don't have an AIDL account yet. Sign up first with “Add to Google Classroom” "
                "on the AIDL website, then come back here.")
    if not user.is_active:
        return "Your AIDL account is switched off. Ask your AIDL admin."
    if user.role != AIDLUser.Role.ADMIN or not user.organization_id:
        return "Only AIDL admins (teachers) can connect Google Classroom classes."
    return ""


def _connect_classes(request, code: str):
    tokens = google_oauth.exchange_tokens(code)
    profile = google_oauth.verify_id_token(tokens["id_token"])
    user = find_user(profile)
    refusal = _teacher_refusal(user)
    if refusal:
        return _message(request, "Can't connect Google Classroom", refusal, 403)
    count = classes.connect(user, profile, tokens)
    org = get_organization_for_user(user)
    if org is not None:  # admins who signed up before sign-in asked for these permissions
        aidl_class.setup(user, org, profile, tokens)
    logger.info("google classroom connected: user %s, %s classes", user.pk, count)
    response = _back(f"Google Classroom connected. Found {count} class{'es' if count != 1 else ''}.")
    return session.set_session(request, response, user)


def _classes_error(request, code: str, description: str):
    logger.info("google classroom connect failed: %s", code)
    return _message(request, "Can't connect Google Classroom", description, 400)


def _message(request, title: str, text: str, status: int = 200, retry_url: str = ""):
    return render(request, "google_classroom/message.html", {
        "title": title, "text": text, "user": None, "org": None,
        "connect_url": retry_url or reverse("google-classroom-classes-connect"),
    }, status=status)


def _back(notice: str):
    return redirect(f"{reverse('google-classroom-classes')}?{urlencode({'notice': notice})}")


@never_cache
@require_GET
def classes_page(request):
    user = session.signed_in(request)
    notice = request.GET.get("notice", "")[:300]
    if user is None:
        return render(request, "google_classroom/classes.html", {
            "user": None, "org": None, "person": None, "notice": notice,
            "connect_url": reverse("google-classroom-classes-connect")})
    refusal = _teacher_refusal(user)
    if refusal:
        return session.clear_session(_message(request, "Google Classroom", refusal, 403))
    return render(request, "google_classroom/classes.html", {
        "user": None, "org": get_organization_for_user(user), "person": user, "notice": notice,
        "data": classes.for_teacher(user), "connect_url": reverse("google-classroom-classes-connect"), "tab": "classes",
        "other_classes": classes.other_classes(user), "invite": _invite_context(user),
        "has_aidl_class": bool(classes.aidl_course_id(user.organization_id)),
        "classroom_link": aidl_class.info(org)["link"] if (org := get_organization_for_user(user)) else "",
        **session.focus_context(request)})


@require_GET
def classes_connect(request):
    if not google_oauth.configured():
        return _message(request, "Can't connect Google Classroom",
                        "Google Classroom isn't set up on the server yet (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).",
                        503)
    state = create_oauth_state("organization", payload=CLASSES)
    user = session.signed_in(request)
    return HttpResponseRedirect(google_oauth.auth_url(state, classroom=True, login_hint=user.email if user else ""))


@require_POST
def classes_sync(request):
    user = session.signed_in(request)
    refusal = _teacher_refusal(user)
    if refusal:
        return _message(request, "Google Classroom", refusal, 403)
    try:
        count = classes.sync(user)
    except GoogleError as exc:
        return _back(str(exc))
    if (org := get_organization_for_user(user)) is not None:
        aidl_class.refresh_later(org)  # cards, answers, licences
    return _back(f"Up to date. {count} class{'es' if count != 1 else ''}.")


@require_POST
def classes_choose(request):
    user = session.signed_in(request)
    refusal = _teacher_refusal(user)
    if refusal:
        return _message(request, "Google Classroom", refusal, 403)
    on = request.POST.get("on") == "1"
    try:
        counts = classes.choose(user, request.POST.get("course_id", ""), on)
    except GoogleError as exc:
        return _back(str(exc))
    if (org := get_organization_for_user(user)) is not None:
        aidl_class.refresh_later(org)  # step 3 may be done now
    return _back(f"Class added to AIDL: {classes.summary(counts)}." if on
                 else "Class taken out of AIDL. Its students keep their AIDL accounts.")


def _invite_context(user) -> dict:
    from .plan_limits import limits, student_room, summary

    org = get_organization_for_user(user)
    if org is None:
        return {}
    return {"room": student_room(org), "teachers_only": limits(org)["students"] == 0, "label": summary(org)["label"],
            "enterprise": classes.bulk_invite_allowed(org)}


@require_POST
def classes_invite(request):
    """Step 3: invite every student of the teacher's ticked classes to the AIDL class."""
    user = session.signed_in(request)
    refusal = _teacher_refusal(user)
    if refusal:
        return _message(request, "Google Classroom", refusal, 403)
    try:
        counts = classes.invite_from_classes(user, request.POST.getlist("course_ids"))
    except GoogleError as exc:
        return _back(str(exc))
    return _back(classes.invite_summary(counts))


@require_POST
def classes_invite_emails(request):
    """Step 3: invite a pasted list of student emails to the AIDL class."""
    user = session.signed_in(request)
    refusal = _teacher_refusal(user)
    if refusal:
        return _message(request, "Google Classroom", refusal, 403)
    try:
        counts = classes.invite_emails(user, request.POST.get("emails", "")[:200_000])
    except GoogleError as exc:
        return _back(str(exc))
    return _back(classes.invite_summary(counts))


@require_POST
def classes_setup(request):
    """Step 4: approve the Traffic Light Check / Highway Code, start the programme."""
    user = session.signed_in(request)
    refusal = _teacher_refusal(user)
    if refusal:
        return _message(request, "Google Classroom", refusal, 403)
    org = get_organization_for_user(user)
    if org is None:
        return _message(request, "Google Classroom", "Your AIDL organization is switched off.", 403)
    from .admin_center import can
    if not can(org, user)["cards"]:
        return _back("Your admin permissions don't include approving cards.")
    notice = flow.teacher_action(user, org, request.POST) or "Nothing changed."
    aidl_class.refresh_later(org)
    return _back(notice)


@require_POST
def classes_signout(request):
    return session.clear_session(redirect("google-classroom-classes"))


# --- step 2, JSON for the frontend (AIDL access token from sign-in) ---

@api_view(["GET"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def api_classes(request):
    refusal = _teacher_refusal(request.user)
    if refusal:
        return Response({"error": "not_allowed", "message": refusal}, status=403)
    data = classes.for_teacher(request.user)
    data["connect_url"] = request.build_absolute_uri(reverse("google-classroom-classes-connect"))
    return Response(data)


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def api_classes_sync(request):
    refusal = _teacher_refusal(request.user)
    if refusal:
        return Response({"error": "not_allowed", "message": refusal}, status=403)
    try:
        classes.sync(request.user)
    except GoogleError as exc:
        return Response({"error": exc.code, "message": str(exc)}, status=409 if exc.code == "reconnect" else 502)
    return Response(classes.for_teacher(request.user))


@api_view(["POST"])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def api_classes_choose(request):
    refusal = _teacher_refusal(request.user)
    if refusal:
        return Response({"error": "not_allowed", "message": refusal}, status=403)
    on = bool(request.data.get("in_programme"))
    try:
        counts = classes.choose(request.user, str(request.data.get("course_id", "")), on)
    except GoogleError as exc:
        return Response({"error": exc.code, "message": str(exc)}, status=400)
    data = classes.for_teacher(request.user)
    data["result"] = counts
    data["message"] = classes.summary(counts) if on else "Class taken out of AIDL."
    return Response(data)


# --- step 4: learners (the link the teacher shares in Google Classroom) ---

def _learn_back(notice: str = ""):
    url = reverse("google-classroom-learn")
    return redirect(f"{url}?{urlencode({'notice': notice[:300]})}" if notice else url)


def _learn_error(request, code: str, description: str):
    logger.info("google classroom learner sign-in failed: %s", code)
    return _message(request, "Can't open AIDL", description, 400, retry_url=reverse("google-classroom-learn"))


def _learner_signed_in(request, code: str):
    profile = google_oauth.verify_id_token(google_oauth.exchange_code(code))
    user = find_user(profile)
    retry = reverse("google-classroom-learn")
    if user is None or not user.organization_id:
        return _message(request, "You're not in AIDL yet",
                        "Your teacher hasn't added your Google Classroom class to AIDL yet, or you signed in with "
                        "a different Google account than the one you use in Classroom.", 403, retry_url=retry)
    if not user.is_active:
        return _message(request, "Can't open AIDL", "Your AIDL account is switched off. Ask your teacher.", 403,
                        retry_url=retry)
    target = reverse("google-classroom-admin") if user.role == AIDLUser.Role.ADMIN else retry
    return session.set_session(request, redirect(target), user)


@never_cache
@require_GET
def learn(request):
    """Signed in -> the learner flow (admins -> their classes page). Not signed in -> Google."""
    user = session.signed_in(request)
    if user is None:
        if not google_oauth.configured():
            return _message(request, "Can't open AIDL", "Google sign-in isn't set up on the server yet.", 503,
                            retry_url=reverse("google-classroom-learn"))
        state = create_oauth_state("organization", payload=LEARN)
        return HttpResponseRedirect(google_oauth.auth_url(state))
    if user.role == AIDLUser.Role.ADMIN:
        return redirect("google-classroom-admin")
    org = get_organization_for_user(user)
    if org is None:
        return session.clear_session(_message(request, "You're not in the AIDL programme",
                                              "Ask your teacher to add your class to AIDL.", 403))
    ctx = flow.learner_context(user, org)
    if "blocked" in ctx:
        return _message(request, *ctx["blocked"], retry_url=reverse("google-classroom-learn"))
    return render(request, "google_classroom/learner.html", {
        **ctx, "person": user, "org": org, "user": None, "course": "",
        "notice": request.GET.get("notice", "")[:300]})


@never_cache
@require_POST
def learn_action(request):
    user = session.signed_in(request)
    org = get_organization_for_user(user) if user else None
    if user is None or org is None:
        return _learn_back()
    return _learn_back(flow.learner_action(user, org, request.POST))
