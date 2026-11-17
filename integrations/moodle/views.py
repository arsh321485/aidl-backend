"""AIDL inside Moodle.

    /api/moodle/login/    Moodle's "Initiate login URL" (LTI step 1)
    /api/moodle/launch/   Moodle's "Tool URL" / "Redirection URI" (LTI step 2)
    /api/moodle/          the signed-in page: learners get their cards and
                          licence (flow.py); admins get the "start" box
    /api/moodle/action/   a learner's button (POST)
    /api/moodle/setup/    an admin's button (POST)

A person clicks the AIDL activity in their Moodle course → Moodle calls
login/ → we bounce them back to Moodle → Moodle posts a signed token to
launch/ → we check it, find or create the AIDL user, and set a cookie.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from api.models import AIDLUser
from api.org_service import get_organization_for_user
from integrations.lti_core.launch import LtiError, login_redirect, verify_launch

from . import accounts, flow, session

logger = logging.getLogger(__name__)
KIND = "moodle"


def launch_url() -> str:
    return f"{settings.BACKEND_URL}/api/moodle/launch/"


def _message(request, title: str, text: str, status: int = 200, org=None):
    return render(request, "moodle/message.html", {"title": title, "text": text, "user": None, "org": org,
                                                   "notice": request.GET.get("notice", "")[:300]}, status=status)


def connect(request):
    """GET /api/moodle/connect/ — the website's "Add to Moodle" button lands here:
    how a Moodle site admin installs AIDL (LTI starts inside Moodle, so there's
    no login to begin from the website)."""
    base = settings.BACKEND_URL.rstrip("/")
    return render(request, "moodle/connect.html", {
        "user": None, "org": None, "launch_url": launch_url(),
        "login_url": f"{base}/api/moodle/login/", "jwks_url": f"{base}/api/lti/jwks/",
    })


@csrf_exempt  # Moodle posts here from its own site
@xframe_options_exempt  # Moodle can show AIDL inside the course page
@never_cache
def login(request):
    params = request.POST if request.method == "POST" else request.GET
    try:
        return HttpResponseRedirect(login_redirect(params, KIND, launch_url()))
    except LtiError as exc:
        return _message(request, "Can't open AIDL", str(exc), 400)


@csrf_exempt
@xframe_options_exempt
@never_cache
@require_POST
def launch(request):
    try:
        data = verify_launch(request.POST)
        user, org = accounts.sign_in(data)
    except LtiError as exc:
        return _message(request, "Can't open AIDL", str(exc), 403)
    logger.info("moodle launch: user %s org %s course %s", user.pk, org.pk, data.context_id)
    response = _home(request, user, org, course=data.context_title)
    return session.set_session(request, response, user)


def _signed_in(request):
    """(user, org), or (None, response) to return."""
    user = session.signed_in(request)
    if user is None:
        return None, _message(request, "Open AIDL from Moodle", "Go to your Moodle course and click the AIDL activity.",
                              401)
    org = get_organization_for_user(user)
    if org is None:
        return None, _message(request, "You're not in the AIDL programme", "Open AIDL from your Moodle course again.",
                              403)
    return (user, org), None


@xframe_options_exempt
@never_cache
def home(request):
    found, error = _signed_in(request)
    if error:
        return error
    return _home(request, *found)


def _home(request, user: AIDLUser, org, course: str = ""):
    ctx = {"person": user, "org": org, "course": course, "user": None,
           "notice": request.GET.get("notice", "")[:300],
           "is_owner": org is not None and org.owner_user_id == str(user.pk)}
    if user.role == AIDLUser.Role.ADMIN:
        return render(request, "moodle/teacher.html", {**ctx, **flow.teacher_context(org)})
    learner = flow.learner_context(user, org)
    if "blocked" in learner:
        return _message(request, *learner["blocked"], org=org)
    return render(request, "moodle/learner.html", {**ctx, **learner})


def _back(notice: str):
    url = "/api/moodle/"
    return redirect(f"{url}?notice={quote(notice[:300], safe='')}" if notice else url)


@xframe_options_exempt
@never_cache
@require_POST
def action(request):
    found, error = _signed_in(request)
    if error:
        return error
    return _back(flow.learner_action(*found, request.POST))


@xframe_options_exempt
@never_cache
@require_POST
def setup(request):
    found, error = _signed_in(request)
    if error:
        return error
    return _back(flow.teacher_action(*found, request.POST))
