"""Step 5: Admin Center pages for Google Classroom admins (core part).

GET  /api/google-classroom/admin/               home: plan, the 5 setup steps, student link
POST /api/google-classroom/admin/action/        approve a starter card / start / skip the admins step
GET  /api/google-classroom/admin/step/<n>/       Classwork's "Step n" (teacher-only draft) → that step's page
GET  /api/google-classroom/admin/team/          learners' progress; remove / add back
POST /api/google-classroom/admin/team/action/
GET  /api/google-classroom/admin/admins/        add / remove admins, permissions
POST /api/google-classroom/admin/admins/action/
GET  /api/google-classroom/admin/plans/         → the website's plans page (signed in)
GET  /api/google-classroom/admin/coverage.csv

Signed in with the same cookie as the classes page (session.py). Not signed
in → Google sign-in (identity only) → back here.
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from api.models import AIDLUser
from api.org_service import get_organization_for_user
from integrations.moodle import flow

from . import admin_center, session


def _message(request, title: str, text: str, status: int = 200):
    return render(request, "google_classroom/message.html", {
        "title": title, "text": text, "user": None, "org": None,
        "connect_url": reverse("google-classroom-admin")}, status=status)


def _admin_page(view):
    """Signed-in AIDL admin → view(request, user, org); else sign in / refuse."""
    @wraps(view)
    @never_cache
    def wrapper(request, *args, **kwargs):
        user = session.signed_in(request)
        if user is None:
            return redirect("google-classroom-learn")  # Google sign-in; admins come back to the Admin Center
        if user.role != AIDLUser.Role.ADMIN or not user.organization_id:
            return _message(request, "Admins only", "The Admin Center is for your school's AIDL admins.", 403)
        org = get_organization_for_user(user)
        if org is None:
            return _message(request, "Admins only", "Your AIDL organization is switched off.", 403)
        response = view(request, user, org, *args, **kwargs)
        if request.method == "POST":  # something may have changed: update the AIDL class in Classroom
            from .aidl_class import refresh_later
            refresh_later(org)
        return response
    return wrapper


def _back(name: str, notice: str):
    url = reverse(name)
    return redirect(f"{url}?{urlencode({'notice': notice[:300]})}" if notice else url)


def _render(request, user, org, template: str, **ctx):
    return render(request, f"google_classroom/{template}", {
        "user": None, "org": org, "person": user, "classroom_link": admin_center.aidl_class_info(org)["link"],
        **session.focus_context(request),
        "notice": request.GET.get("notice", "")[:300], **ctx})


@_admin_page
def home(request, user, org):
    return _render(request, user, org, "admin.html", **admin_center.home(org, user),
                   learn_url=request.build_absolute_uri(reverse("google-classroom-learn")))


@_admin_page
def step(request, user, org, n: int):
    from .aidl_class import STEPS, step_url

    if not 1 <= n <= len(STEPS):
        return redirect("google-classroom-admin")
    return session.set_focus_step(request, redirect(step_url(n)), n)


@require_POST
@_admin_page
def action(request, user, org):
    from api import admin_setup

    act, can = request.POST.get("action", ""), admin_center.can(org, user)
    if act == "skip_admins":
        if not can["admins"]:
            return _back("google-classroom-admin", "Only admins who manage admins can skip this step.")
        admin_setup.update(org, admins_step="skipped")
        return _back("google-classroom-admin", "Admins step skipped. You can add admins any time.")
    if act in ("approve", "start"):
        if not can["cards"]:
            return _back("google-classroom-admin", "Your admin permissions don't include approving cards.")
        return _back("google-classroom-admin", flow.teacher_action(user, org, request.POST) or "Nothing changed.")
    return _back("google-classroom-admin", "")


@_admin_page
def team(request, user, org):
    return _render(request, user, org, "team.html", **admin_center.team(org))


@require_POST
@_admin_page
def team_action(request, user, org):
    act, uid = request.POST.get("action", ""), request.POST.get("user_id", "")
    if act == "remove":
        notice = admin_center.remove_learner(org, user, uid)
    elif act == "restore":
        notice = admin_center.restore_learner(org, user, uid)
    else:
        notice = ""
    return _back("google-classroom-admin-team", notice)


@_admin_page
def admins(request, user, org):
    return _render(request, user, org, "admins.html", **admin_center.admins(org, user),
                   can=admin_center.can(org, user))


@require_POST
@_admin_page
def admins_action(request, user, org):
    act, uid, chosen = request.POST.get("action", ""), request.POST.get("user_id", ""), request.POST.getlist("perm")
    if act == "add":
        notice = admin_center.add_admin(org, user, uid, chosen)
    elif act == "perms":
        notice = admin_center.set_permissions(org, user, uid, chosen)
    elif act == "remove":
        notice = admin_center.remove_admin(org, user, uid)
    else:
        notice = ""
    return _back("google-classroom-admin-admins", notice)


@_admin_page
def plans(request, user, org):
    from api.auth_jwt import create_access_token

    if not admin_center.can(org, user)["billing"]:
        return _back("google-classroom-admin", "Your admin permissions don't include the plan.")
    return HttpResponseRedirect(f"{settings.FRONTEND_URL.rstrip('/')}/plans?token={create_access_token(user)}")


@_admin_page
def upgrade(request, user, org):
    """Classwork's "⭐ Plan" item: the website's plans page (choose a plan → topics → checkout),
    or a one-step page saying who can upgrade."""
    if not admin_center.can(org, user)["billing"]:
        return _message(request, "Upgrade plan", "Only the master admin, or admins allowed to manage the plan, "
                                                 "can upgrade. Ask your master admin.", 403)
    return plans(request)


@_admin_page
def coverage(request, user, org):
    from api.slack_cards import coverage_csv, coverage_data

    response = HttpResponse(coverage_csv(coverage_data(org)["rows"]), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="aidl-coverage.csv"'
    return response
