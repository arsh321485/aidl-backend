"""Enterprise "whole school" pages (school_directory.py).

GET  /api/google-classroom/admin/school/           the page: connect · which folders are students ·
                                                   count · "Send AIDL to all students" · progress
POST /api/google-classroom/admin/school/action/    count | send | recheck | disconnect
GET  /api/google-classroom/admin/school/connect/   → Google, asking for the whole-school permissions
GET  /api/google-classroom/school/it-admin/?t=…    the link a teacher sends to their IT admin (no AIDL
                                                   sign-in needed): what AIDL asks for, and a button
GET  /api/google-classroom/school/it-admin/go/?t=… → Google, for that school
Google comes back to views.callback with the state payload "google_classroom:school|<link token>".
"""

from __future__ import annotations

import logging

from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from api.microsoft_auth import create_oauth_state

from . import google_oauth, school_directory, session
from .admin_views import _admin_page, _back, _render
from .google_oauth import GoogleError

logger = logging.getLogger(__name__)

SCHOOL = "google_classroom:school"  # the OAuth state payload prefix
PAGE = "google-classroom-admin-school"


def _message(request, title: str, text: str, status: int = 400, retry_url: str = ""):
    return render(request, "google_classroom/message.html", {
        "title": title, "text": text, "user": None, "org": None,
        "connect_url": retry_url or reverse(PAGE)}, status=status)


def _to_google(token: str, login_hint: str = ""):
    if not google_oauth.configured():
        raise GoogleError("google_not_configured", "Google sign-in isn't set up on the server yet.")
    state = create_oauth_state("organization", payload=f"{SCHOOL}|{token}")
    return redirect(google_oauth.school_auth_url(state, login_hint=login_hint))


# --- the Admin Center page ---

@_admin_page
def page(request, user, org):
    ctx = school_directory.page(org)
    if ctx["enterprise"] and ctx["domain"]:
        ctx["it_admin_link"] = request.build_absolute_uri(
            f"{reverse('google-classroom-school-it-admin')}?t={school_directory.link_token(org, ctx['domain'])}")
    return _render(request, user, org, "school.html", tab="school", **ctx)


@_admin_page
def connect(request, user, org):
    domain = school_directory.school_domain(org)
    if not school_directory.allowed(org) or not domain:
        return _back(PAGE, "")
    try:
        return _to_google(school_directory.link_token(org, domain), login_hint=user.email)
    except GoogleError as exc:
        return _back(PAGE, str(exc))


@require_POST
@_admin_page
def action(request, user, org):
    act = request.POST.get("action", "")
    try:
        if act == "count":
            n = school_directory.count(org, request.POST.getlist("unit"))
            return _back(PAGE, f"{n} student{'s' if n != 1 else ''} found.")
        if act in ("send", "recheck"):
            school_directory.start(org)
            return _back(PAGE, "Adding your students to the AIDL class. This page shows the progress.")
        if act == "disconnect":
            school_directory.disconnect(org)
            return _back(PAGE, "Whole school disconnected. Students already added stay in the AIDL class.")
    except GoogleError as exc:
        return _back(PAGE, str(exc))
    return _back(PAGE, "")


# --- the IT admin's link ---

@require_GET
@never_cache
def it_admin(request):
    token = request.GET.get("t", "")
    try:
        org, domain = school_directory.read_link_token(token)
    except GoogleError as exc:
        return _message(request, "Link not valid", str(exc), 400, retry_url="https://classroom.google.com/")
    return render(request, "google_classroom/school_it_admin.html", {
        "user": None, "org": org, "school": org.name, "domain": domain,
        "go_url": f"{reverse('google-classroom-school-it-admin-go')}?t={token}"})


@require_GET
def it_admin_go(request):
    token = request.GET.get("t", "")
    try:
        school_directory.read_link_token(token)
        return _to_google(token)
    except GoogleError as exc:
        return _message(request, "Can't connect the whole school", str(exc), 400,
                        retry_url=f"{reverse('google-classroom-school-it-admin')}?t={token}")


# --- Google comes back (views.callback) ---

def connected(request, code: str, token: str):
    retry = f"{reverse('google-classroom-school-it-admin')}?t={token}"
    try:
        org, domain = school_directory.read_link_token(token)
        tokens = google_oauth.exchange_tokens(code)
        profile = google_oauth.verify_id_token(tokens["id_token"])
        user = school_directory.connect(org, domain, profile, tokens)
    except GoogleError as exc:
        logger.info("google classroom: whole school connect refused: %s", exc.code)
        text = str(exc)
        if exc.code == "not_admin":
            text += " If you're a teacher, send your IT admin the link on AIDL's Whole school page."
        return _message(request, "Can't connect the whole school", text, 403 if exc.code == "not_admin" else 400,
                        retry_url=retry)
    response = _back(PAGE, "Whole school connected. Now tick the folders that hold your students.")
    return session.set_session(request, response, user)
