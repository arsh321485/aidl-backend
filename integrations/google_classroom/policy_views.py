"""Step 6: the "Your AI policy" part of the Admin Center (Google Classroom).

GET/POST /api/google-classroom/admin/policy/            company (countries, industry) → the 14 topics
GET/POST /api/google-classroom/admin/policy/<n>/        one policy card: questions, preview, approve
GET      /api/google-classroom/admin/regulators/
GET/POST /api/google-classroom/admin/upload/            own AUP → 14 cards (paid plans)
POST     /api/google-classroom/admin/upload/publish/
GET/POST /api/google-classroom/admin/logo/              logo on cards and licences (paid plans)
GET/POST /api/google-classroom/admin/apps/?kind=ai|it   the AI / IT apps your learners may use

Same rules and the same api/ modules as the Teams Admin Center
(api/teams_portal.py); only the pages are ours. Approved cards reach Google
Classroom learners in their flow (/learn/) the next time they open it: the
shared delivery only posts to Slack / Teams people, so nothing is posted.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from api import admin_setup, aup_store, aup_upload, licence_image, regulators
from api import policy_cards as pc
from api.models import AupUpload, RegisteredApp
# The Teams Admin Center's form helpers for a policy card (generic: they only
# turn a POST into answers and answers into questions).
from api.teams_portal import _day, _policy_context, _read_policy

from .admin_views import _admin_page, _back, _message, _render

LOGO_MAX_BYTES = 5 * 1024 * 1024
PAID = ("basic", "enterprise")


def _can(org, user) -> dict:
    from api import admin_perms

    master = admin_perms.is_master(org, user)
    return {
        "policy": master or bool(user.perm_policy) or bool(user.perm_approve_apps),
        "apps": admin_perms.has(org, user, "approve_apps"),
        "logo": admin_perms.has(org, user, "manage_admins"),
    }


def _denied(request):
    return _message(request, "Your admin permissions don't include this", "Ask your master admin.", 403)


def _when_note(org, send_on: str) -> str:
    if not admin_setup.started(org):
        return " Learners get it once the programme starts."
    return (f" Learners see it from {_day(send_on)}." if send_on
            else " Learners see it the next time they open AIDL.")


# --- policy cards ---

@_admin_page
def policy(request, user, org):
    if not _can(org, user)["policy"]:
        return _denied(request)
    co = aup_store.company(org)
    if request.method == "POST":
        countries = [c for c in request.POST.getlist("countries") if c in regulators.COUNTRIES]
        industry = request.POST.get("industry", "")
        if not countries or industry not in regulators.INDUSTRIES:
            return redirect(f"{_url('google-classroom-admin-policy')}?" + urlencode(
                {"company": 1, "notice": "Choose at least one country and your industry."}))
        aup_store.save_company(org, countries, industry)
        return _back("google-classroom-admin-policy", "Saved. Your regulators are ready to view.")
    if not co or request.GET.get("company"):
        return _render(request, user, org, "policy/company.html", co=co, countries=regulators.COUNTRIES,
                       industries=regulators.INDUSTRIES, tab="policy")
    answers = aup_store.answers(org, with_tools=False)["answers"]
    states = pc.statuses(org)
    topics = []
    # Like Slack and Teams: the Trial includes 3 topics (shown first); the other 11 are paid (api.policy_cards).
    for n in pc.ordered(org):
        st = states.get(n) or {"state": "new"}
        qs = len(pc.questions(n, answers)) + (1 if n == 7 else 0)
        topics.append({"n": n, "title": pc.SHORT[n], "icon": pc.ICONS[n], "state": st["state"],
                       "agreed": st.get("agreed", 0), "day": _day(st.get("send_on")), "qs": qs,
                       "locked": pc.is_locked(org, n), "free": n in pc.FREE_TOPICS})
    return _render(request, user, org, "policy/policy.html", topics=topics, done=len(states),
                   upload_allowed=aup_upload.can_upload(org), tab="policy",
                   on_trial=any(t["locked"] for t in topics), **_upsell(org, user))


def _upsell(org, user) -> dict:
    from api.policy_cards_slack import PERKS

    from .admin_center import can

    return {"perks": PERKS, "can_upgrade": can(org, user)["billing"]}


def _url(name, *args):
    from django.urls import reverse

    return reverse(name, args=args)


@_admin_page
def policy_card(request, user, org, n: int):
    if not _can(org, user)["policy"]:
        return _denied(request)
    if n not in pc.TOPICS:
        return redirect("google-classroom-admin-policy")
    if pc.is_locked(org, n):  # a paid topic on the Trial: never approved, also not by a direct POST
        return _render(request, user, org, "policy/policy_locked.html", title=pc.SHORT[n], icon=pc.ICONS[n],
                       tab="policy", **_upsell(org, user))
    a, when, day, errors = aup_store.answers(org), "today", "", {}
    if request.method == "POST":
        post = request.POST.copy()
        post["n"] = str(n)
        a, when, day = _read_policy(org, post)
        if post.get("action") == "approve":
            errors = pc.problems(n, a)
            send_on = ""
            if when == "day":
                why = pc.check_day(day)
                if why:
                    errors["day"] = why
                elif day != timezone.localdate().isoformat():
                    send_on = day
            if not errors:
                pc.approve(org, user, n, a, send_on)
                return _back("google-classroom-admin-policy",
                             f"Policy card “{pc.SHORT[n]}” approved." + _when_note(org, send_on))
        aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
    return _render(request, user, org, "policy/policy_card.html", n=n, title=pc.SHORT[n], icon=pc.ICONS[n],
                   existing=aup_store.card(org, n), errors=list(errors.values()), when=when,
                   day=day or (timezone.localdate() + timedelta(days=1)).isoformat(), tab="policy",
                   **_policy_context(org, n, a))


@_admin_page
def regulators_page(request, user, org):
    if not _can(org, user)["policy"]:
        return _denied(request)
    co = aup_store.company(org)
    return _render(request, user, org, "policy/regulators.html", co=co, tab="policy",
                   where=", ".join(co.get("countries") or []) or "—",
                   regs=regulators.regulators_for(co.get("countries") or [], co.get("industry", "")))


# --- own AUP upload ---

@_admin_page
def upload(request, user, org):
    if not _can(org, user)["policy"]:
        return _denied(request)
    if not aup_upload.can_upload(org):
        return _message(request, "A paid feature", "Uploading your own AUP is part of the Basic and Enterprise plans.")
    if request.method == "POST":
        upfile = request.FILES.get("file")
        if upfile is None:
            return _back("google-classroom-admin-upload", "Choose your policy file.")
        if upfile.size > settings.AUP_UPLOAD_MAX_BYTES:
            return _back("google-classroom-admin-upload", "That file is too big.")
        try:
            aup_upload.start(org, user, upfile.name, upfile.read())
        except aup_upload.UploadError as exc:
            return _back("google-classroom-admin-upload", exc.message)
        return redirect("google-classroom-admin-upload")
    latest = aup_upload.latest(org)
    if request.GET.get("new") or latest is None:
        return _render(request, user, org, "policy/upload.html", form=True, has_logo=bool(org.logo_data), tab="policy")
    cards = aup_upload.cards_of(latest)
    return _render(request, user, org, "policy/upload.html", upload=latest, cards=cards, tab="policy",
                   warn=aup_upload.warnings(cards),
                   processing=latest.status == AupUpload.Status.PROCESSING,
                   failed=latest.status == AupUpload.Status.FAILED,
                   published=latest.status == AupUpload.Status.PUBLISHED)


@require_POST
@_admin_page
def upload_publish(request, user, org):
    if not _can(org, user)["policy"]:
        return _denied(request)
    found = AupUpload.objects.filter(pk=request.POST.get("upload_id"), organization_id=str(org.pk)).first()
    if found is None:
        return redirect("google-classroom-admin-upload")
    send_on = ""
    if request.POST.get("when") == "day":
        day = request.POST.get("day", "")
        why = pc.check_day(day)
        if why:
            return _back("google-classroom-admin-upload", why)
        send_on = "" if day == timezone.localdate().isoformat() else day
    try:
        aup_upload.publish(org, found, user, send_on)
    except aup_upload.UploadError as exc:
        return _back("google-classroom-admin-upload", exc.message)
    return _back("google-classroom-admin-policy",
                 f"The 14 policy cards from {found.file_name} are approved." + _when_note(org, send_on))


# --- logo ---

@_admin_page
def logo(request, user, org):
    if not _can(org, user)["logo"]:
        return _denied(request)
    if org.plan not in PAID:
        return _message(request, "A paid feature", "Your school's logo on cards and licences is part of the Basic "
                        "and Enterprise plans.")
    if request.method != "POST":
        return _render(request, user, org, "policy/logo.html", tab="home")
    upfile = request.FILES.get("file")
    if upfile is None or upfile.size > LOGO_MAX_BYTES:
        return _back("google-classroom-admin-logo", "Choose a PNG or JPG image under 5 MB.")
    try:
        org.logo_data = licence_image.save_logo(upfile.read())
    except Exception:  # noqa: BLE001 — anything Pillow can't read
        return _back("google-classroom-admin-logo", "AIDL couldn't read that logo file. Please upload a PNG or JPG image.")
    org.save(update_fields=["logo_data", "updated_at"])
    return _back("google-classroom-admin", "Your logo is saved. It now appears on every licence and card.")


# --- AI / IT apps ---

@_admin_page
def apps(request, user, org):
    from api.admin_ops import AdminOpsError, add_registered_app
    from api.slack_cards import AI_CATEGORIES, DATA_ALLOWED, IT_CATEGORIES
    from api.slack_modals import _DATA_VALUE

    if not _can(org, user)["apps"]:
        return _denied(request)
    kind = "it" if request.GET.get("kind") == "it" or request.POST.get("kind") == "it" else "ai"
    app_type = RegisteredApp.AppType.AI if kind == "ai" else RegisteredApp.AppType.IT
    if request.method == "POST":
        status = (RegisteredApp.Status.APPROVED if request.POST.get("status") == "Approved"
                  else RegisteredApp.Status.REJECTED)
        try:
            add_registered_app(org, app_type=app_type, name=request.POST.get("name", "").strip(),
                               category=request.POST.get("category") or "Other",
                               data_allowed=_DATA_VALUE.get(request.POST.get("data", ""), ""), status=status)
        except AdminOpsError as exc:
            notice = exc.message or exc.code
        else:
            notice = "Added."
        return redirect(f"{_url('google-classroom-admin-apps')}?" + urlencode({"kind": kind, "notice": notice}))
    rows = RegisteredApp.objects.filter(organization_id=str(org.pk), is_active=True, app_type=app_type).order_by("name")
    return _render(request, user, org, "policy/apps.html", kind=kind, apps=rows, tab="policy",
                   data=[d for d, _c in DATA_ALLOWED], categories=AI_CATEGORIES if kind == "ai" else IT_CATEGORIES)
