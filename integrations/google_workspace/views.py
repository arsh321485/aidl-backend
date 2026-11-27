"""AIDL for Google Workspace (step 1): sign up / sign in with a Google Workspace
account, one organization per company domain, the admin's "start" page and the
learner flow.

GET  /api/google-workspace/login/?redirect=1
     The website's "Sign up with Google Workspace": {"auth_url", "mode"} (or,
     with redirect=1, straight to Google). The first person of a company
     domain becomes its AIDL admin.
GET  /api/google-workspace/join/
     The link an admin shares with their team: straight to Google, then the
     learner's AIDL page.
GET  /api/google-workspace/callback/
     Google sends the browser back here. We sign the person in (the
     "aidl_gworkspace" cookie), email them their next step (mail.py) and open
     Gmail, where AIDL for Google Workspace lives.
GET  /api/google-workspace/go/?t=...  an email's button: signs the person in
                                     (links.py) and opens that step
GET  /api/google-workspace/          the signed-in page: admins get the
                                     "start" box, learners their cards and licence
POST /api/google-workspace/action/   a learner's button
POST /api/google-workspace/setup/    an admin's button (approve a starter card / start)
POST /api/google-workspace/invite/   an admin emails colleagues a link to join
POST /api/google-workspace/remind/   an admin re-sends one person's (or everyone's) current email
GET  /api/google-workspace/export/   an admin downloads the team's coverage CSV
POST /api/google-workspace/signout/

The learner flow and the start box are the same as Moodle's and Google
Classroom's (integrations/moodle/flow.py, which only calls api/).
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from api.microsoft_auth import consume_oauth_state_with_payload, create_oauth_state
from api.models import AIDLUser
from api.org_service import get_organization_for_user
from integrations.moodle import flow

from . import accounts, google_auth, links, mail, ops, progress, session
from .google_auth import GoogleError

logger = logging.getLogger(__name__)

MODE = "google_workspace"  # the OAuth state payload of a website sign-up / sign-in
JOIN = "google_workspace:join"  # ... of a team member opening the admin's link


def _backend(path: str) -> str:
    return f"{settings.BACKEND_URL.rstrip('/')}{path}"


# ---------- sign in ----------

@require_GET
def login(request):
    if not google_auth.configured():
        return JsonResponse({"error": "google_not_configured",
                             "message": "Google Workspace sign-in isn't set up on the server yet "
                                        "(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)."}, status=503)
    state = create_oauth_state("organization", payload=MODE)
    url = google_auth.auth_url(state)
    if request.GET.get("redirect") == "1":
        return HttpResponseRedirect(url)
    return JsonResponse({"auth_url": url, "state": state, "enroll_as": "organization", "mode": MODE})


@require_GET
def join(request):
    if not google_auth.configured():
        return _message(request, "Can't open AIDL", "Google Workspace sign-in isn't set up on the server yet.", 503)
    return HttpResponseRedirect(google_auth.auth_url(create_oauth_state("organization", payload=JOIN)))


@require_GET
def callback(request):
    """Website sign-ups and team members' joins both land on the AIDL page.
    (Not the website's /auth/callback: that page is shared by every platform,
    and this folder leaves it alone.)"""
    _enroll_as, payload, state_error = consume_oauth_state_with_payload(request.GET.get("state", ""))
    if request.GET.get("error"):
        return _error(request, "Google sign-in was cancelled.")
    if state_error or payload not in (MODE, JOIN):  # unknown, expired, reused, or another platform's state
        return _error(request, "Your sign-in session expired. Please try again.")
    code = request.GET.get("code")
    if not code:
        return _error(request, "Google did not return a sign-in code.")
    try:
        profile = google_auth.profile_from_code(code)
        user, org = accounts.sign_in(profile)
    except GoogleError as exc:
        return _error(request, str(exc), exc.code)
    logger.info("google workspace sign-in: user %s org %s (%s)", user.pk, org.pk, payload)
    if user.role == AIDLUser.Role.ADMIN:
        sent = mail.admin_center(user, org)
    else:
        sent = mail.send_next(user, org) or mail.has_email(user)
    # AIDL lives in Gmail: open it, where the email is waiting. Without a real mail
    # service (local testing, emails only in the console), open the AIDL page instead.
    to = mail.gmail_url(user.email) if sent and _emails_delivered() else reverse("google-workspace-home")
    return session.set_session(request, redirect(to), user)


def _emails_delivered() -> bool:
    return "console" not in settings.EMAIL_BACKEND


@require_GET
def go(request):
    """An email's button: sign in as that person and open the step."""
    user, path = links.open_link(request.GET.get("t", ""))
    if user is None:
        return _error(request, "This link has expired. Sign in with Google to continue.")
    return session.set_session(request, redirect(path), user)


def _error(request, text: str, code: str = ""):
    if code:
        logger.info("google workspace sign-in refused: %s", code)
    return render(request, "google_workspace/message.html", {
        "title": "Can't open AIDL", "text": text, "user": None, "org": None,
        "retry_url": reverse("google-workspace-join"), "website_url": settings.FRONTEND_URL}, status=400)


# ---------- the signed-in page ----------

def _message(request, title: str, text: str, status: int = 200, org=None):
    return render(request, "google_workspace/message.html", {"title": title, "text": text, "user": None, "org": org},
                  status=status)


def _signed_in(request):
    """(user, org), or (None, response) to return."""
    user = session.signed_in(request)
    if user is None:
        return None, HttpResponseRedirect(reverse("google-workspace-join"))
    org = get_organization_for_user(user)
    if org is None:
        return None, _message(request, "You're not in the AIDL programme",
                              "Ask your AIDL admin for the link to join.", 403)
    return (user, org), None


@never_cache
def home(request):
    found, error = _signed_in(request)
    if error:
        return error
    user, org = found
    ctx = {"person": user, "org": org, "course": "", "user": None, "notice": request.GET.get("notice", "")[:300],
           "is_owner": org.owner_user_id == str(user.pk)}
    if user.role == AIDLUser.Role.ADMIN:
        domain = user.email.rsplit("@", 1)[-1] if "@" in user.email else ""
        team = progress.rows(org)
        return render(request, "google_workspace/teacher.html", {
            **ctx, **flow.teacher_context(org), "domain": domain, "join_url": _backend(reverse("google-workspace-join")),
            "team": team, "team_summary": progress.summary(team)})
    from api import admin_setup

    if not admin_setup.started(org):  # flow.py's own message talks about a teacher and a course
        return _message(request, "Nothing to do yet", f"{org.name}'s AIDL admin is still setting things up. "
                        "Open this page again a little later.", org=org)
    mail.send_next(user, org)  # e.g. back from making their avatar: the licence email
    learner = flow.learner_context(user, org)
    if "blocked" in learner:
        return _message(request, *learner["blocked"], org=org)
    return render(request, "google_workspace/learner.html", {**ctx, **learner})


def _back(notice: str, anchor: str = ""):
    url = reverse("google-workspace-home")
    url = f"{url}?notice={quote(notice[:300], safe='')}" if notice else url
    return redirect(f"{url}#{anchor}" if anchor else url)


@never_cache
@require_POST
def action(request):
    found, error = _signed_in(request)
    if error:
        return error
    user, org = found
    return _back(ops.learner(user, org, request.POST))


@never_cache
@require_POST
def setup(request):
    found, error = _signed_in(request)
    if error:
        return error
    user, org = found
    return _back(ops.setup(user, org, request.POST))



@never_cache
@require_POST
def invite(request):
    """The admin's "Invite your team"."""
    found, error = _signed_in(request)
    if error:
        return error
    user, org = found
    return _back(ops.invite(user, org, request.POST.get("emails", "")))


@never_cache
@require_POST
def remind(request):
    """The Admin Center's "Remind" (one person) and "Remind everyone who hasn't finished"."""
    found, error = _signed_in(request)
    if error:
        return error
    user, org = found
    return _back(ops.remind(user, org, request.POST.get("who", "")), "team")


@never_cache
@require_GET
def plans(request):
    """The Admin Center's "See plans": AIDL's plans & payment page, signed in as this admin
    (payment can't happen inside Gmail, so this is the one step on a web page)."""
    from django.conf import settings

    from api.auth_jwt import create_access_token

    found, error = _signed_in(request)
    if error:
        return error
    user, org = found
    if user.role != AIDLUser.Role.ADMIN:
        return _message(request, "Admins only", "Only your AIDL admins can change the plan.", 403)
    return redirect(f"{settings.FRONTEND_URL.rstrip('/')}/plans?token={create_access_token(user)}")


@never_cache
@require_GET
def export(request):
    """The team's coverage CSV: the same columns as the Slack and Teams exports."""
    from django.http import HttpResponse

    from api.slack_cards import coverage_csv, coverage_data

    found, error = _signed_in(request)
    if error:
        return error
    user, org = found
    if user.role != AIDLUser.Role.ADMIN:
        return _message(request, "Admins only", "Only your AIDL admins can download the team report.", 403)
    response = HttpResponse(coverage_csv(coverage_data(org)["rows"]), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="aidl-coverage.csv"'
    return response


@require_POST
def signout(request):
    return session.clear_session(_message(request, "Signed out", "You're signed out of AIDL."))
