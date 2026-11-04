"""AIDL's pages for Teams (no bot, so a Teams card can't act by itself): every
button on a channel card opens one of these. People sign in with Microsoft;
the pages do what Slack's buttons and popups do, through the same modules.

    /api/teams/app/             sends you to your page
    /api/teams/app/me/          employee: Welcome → policy cards, Traffic Light,
                                Highway Code → avatar → licence
    /api/teams/app/me/cards/<no>/   one card + Got it, Menu
    /api/teams/app/admin/       Admin Center: the 6 setup steps and their pages

Sign-in: Microsoft → auth_views.teams_callback → finish_login → an HttpOnly
cookie holding a signed token. The role and organization are read from the
database on every request, so removing an admin takes effect at once.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import wraps
from urllib.parse import quote

import jwt
from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import (admin_perms, admin_setup, app_requests, aup_store, aup_upload, card_ack, card_approval, card_image,
               licence_image, licensing, policy_awareness, regulators, teams_channel)
from . import policy_cards as pc
from .models import AIDLUser, AppRequest, AupUpload, Organization, PackageDelivery, RegisteredApp
from .org_service import get_organization_for_user
from .plans import card_by_no, cadence, usage
from .slack_modals import in_background

logger = logging.getLogger(__name__)
COOKIE = "aidl_teams"
SESSION_DAYS = 14
LOGO_MAX_BYTES = 5 * 1024 * 1024
_SPECIFY = re.compile(r"\s*\(specify[^)]*\)\s*", re.I)


# ---------- sign-in ----------

def _session_token(user: AIDLUser) -> str:
    now = datetime.now(dt_timezone.utc)
    return jwt.encode({"sub": str(user.pk), "type": "teams_app", "iat": now, "exp": now + timedelta(days=SESSION_DAYS)},
                      settings.JWT_SECRET, algorithm="HS256")


def set_session(response, user: AIDLUser):
    response.set_cookie(COOKIE, _session_token(user), max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=not settings.DEBUG, samesite="Lax", path="/api/teams/app/")
    return response


def _signed_in(request) -> AIDLUser | None:
    try:
        data = jwt.decode(request.COOKIES.get(COOKIE, ""), settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if data.get("type") != "teams_app":
        return None
    return AIDLUser.objects.filter(pk=data.get("sub"), is_active=True).first()


def _safe_next(value: str) -> str:
    """Only our own pages: a link can't send someone elsewhere after sign-in."""
    base = reverse("teams-app")
    return value if value.startswith(base) and "//" not in value and "\\" not in value else base


def _message(request, title: str, text: str, status: int = 200, user=None, org=None):
    return render(request, "teams_app/message.html", {"title": title, "text": text, "user": user, "org": org},
                  status=status)


@never_cache
def login(request):
    from .microsoft_auth import build_auth_url, microsoft_configured

    if not microsoft_configured():
        return _message(request, "Not available yet", "Microsoft sign-in isn't set up. Please tell your AIDL admin.", 503)
    data = build_auth_url("individual", "app:" + _safe_next(request.GET.get("next", "")))
    return HttpResponseRedirect(data["auth_url"])


@never_cache
@require_POST
def logout(request):
    response = redirect("teams-app-signed-out")
    response.delete_cookie(COOKIE, path="/api/teams/app/")
    return response


@never_cache
def signed_out(request):
    return _message(request, "You're signed out", "Open AIDL from your Teams channel to sign in again.")


def finish_login(request, token_result: dict, profile: dict, next_url: str):
    """The Microsoft callback for a sign-in started by these pages. It never
    creates a team or an organization: you're either known, or in the org's Team."""
    from .microsoft_auth import in_team, seal_refresh_token

    tenant = (token_result.get("id_token_claims") or {}).get("tid", "")
    token = token_result["access_token"]
    user = AIDLUser.objects.filter(microsoft_id=profile["microsoft_id"], is_active=True).first()
    if user is None and tenant:
        org = Organization.objects.filter(ms_tenant_id=tenant, is_active=True).first()
        if org is not None and org.teams_team_id and in_team(token, org.teams_team_id):
            user = teams_channel.join(org, {"user_id": profile["microsoft_id"], "email": profile.get("email", ""),
                                            "full_name": profile.get("full_name", "")}, token)
            if user is None:
                return _message(request, "This team is full", "Your company's AIDL plan has no free seats. Your AIDL "
                                "admin has been told.", 403)
    if user is None:
        return _message(request, "You're not in the AIDL programme yet",
                        "Ask your AIDL admin to add you to the AIDL team, then open AIDL from Teams again.", 403)
    org = get_organization_for_user(user)
    if org is not None and org.ms_tenant_id and tenant and org.ms_tenant_id != tenant:
        return _message(request, "Wrong account", "This Microsoft account belongs to another company.", 403)
    user.last_login_at = timezone.now()
    fields = ["last_login_at", "updated_at"]
    # Only admins' logins are kept: AIDL posts in the channel with them.
    if user.role == AIDLUser.Role.ADMIN and token_result.get("refresh_token"):
        user.ms_refresh_token = seal_refresh_token(token_result["refresh_token"])
        fields.append("ms_refresh_token")
    user.save(update_fields=fields)
    if user.role == AIDLUser.Role.ADMIN and org is not None:
        teams_channel.forget_token(org)
    return set_session(HttpResponseRedirect(_safe_next(next_url)), user)


def _page(view):
    """Signed in → view(request, user, org, ...); else off to Microsoft."""
    @wraps(view)
    @never_cache
    def wrapper(request, *args, **kwargs):
        user = _signed_in(request)
        if user is None:
            return redirect(f"{reverse('teams-app-login')}?next={quote(request.get_full_path(), safe='')}")
        org = get_organization_for_user(user)
        if not teams_channel.is_teams(org):
            return _message(request, "You're not in the AIDL programme yet",
                            "Ask your AIDL admin to add you to the AIDL team, then open AIDL from Teams again.", 403,
                            user=user)
        teams_channel.dispatch_if_due(org)
        return view(request, user, org, *args, **kwargs)
    return wrapper


def _admin_page(view):
    @wraps(view)
    @_page
    def wrapper(request, user, org, *args, **kwargs):
        if user.role != AIDLUser.Role.ADMIN:
            return _message(request, "Admins only", "This page is for your company's AIDL admins.", 403, user=user, org=org)
        return view(request, user, org, *args, **kwargs)
    return wrapper


def _can(user: AIDLUser) -> dict:
    from .slack_cards import light_admin_context

    return light_admin_context(user)


def _denied(request, user, org):
    return _message(request, "Your admin permissions don't include this", "Ask your master admin.", 403, user=user, org=org)


def _flash(request, text: str, path: str):
    return redirect(f"{path}{'&' if '?' in path else '?'}notice={quote(text[:300], safe='')}")


def _render(request, user, org, template: str, **ctx):
    return render(request, f"teams_app/{template}", {"user": user, "org": org, "notice": request.GET.get("notice", "")[:300],
                                                     "is_admin": user.role == AIDLUser.Role.ADMIN, **ctx})


@_page
def home(request, user, org):
    return redirect("teams-app-admin" if user.role == AIDLUser.Role.ADMIN else "teams-app-me")


# ---------- employee ----------

def _first(user: AIDLUser) -> str:
    return (user.full_name or "there").split()[0]


def _next_card(member: AIDLUser):
    return (PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED,
                                           post_at__gt=timezone.now()).order_by("post_at").first())


@_page
def me(request, user, org):
    from .employee_flow import stage
    from .policy_cards import ICONS, SHORT

    if user.role == AIDLUser.Role.ADMIN:
        return redirect("teams-app-admin")
    if user.slack_left_at:
        return _message(request, "You're no longer in the programme", f"You were removed from {org.name}'s AIDL "
                        "programme, so your licence is suspended. Your AIDL admin can add you back.", user=user, org=org)
    if not admin_setup.started(org):
        return _message(request, "Nothing to do yet", f"{org.name}'s AIDL admin is still setting things up. You'll "
                        "get a message in your Teams channel when it starts.", user=user, org=org)
    teams_channel.deliver_due(org, user)
    now = stage(user, org)
    from .avatar import start_url

    ctx = {"welcome": not user.slack_onboarded_at, "stage": now, "avatar_url": start_url(user), **_menu(user),
           "cadence": cadence(org)[:1].upper() + cadence(org)[1:], "first": _first(user)}
    if ctx["welcome"]:
        ctx["policy_count"] = len(aup_store.required_points(org))
        return _render(request, user, org, "me.html", **ctx)
    versions = aup_store.required_points(org)
    ctx["policy"] = [{"n": n, "title": SHORT.get(n, ""), "icon": ICONS.get(n, ""), "version": versions[n],
                      "image": card_image.image_url(f"aup-{n}", org)} for n in aup_store.pending_points(user, org)]
    for item, kind in (("traffic_light", "traffic"), ("highway_code", "highway")):
        if now == item:
            ctx["waiting"] = not card_approval.is_approved(org, item)
            ctx[kind] = card_image.image_url(item, org)
    if now == "licence" and user.licence_issued and user.avatar_data:
        version = licence_image.url_version(user, org)
        verify = f"{settings.FRONTEND_URL.rstrip('/')}/home#verify?lic={user.licence_number}"
        text = quote(f"I just earned my AI Driving Licence from {org.name}!")
        link = quote(verify, safe="")
        ctx["licence"] = {
            "link": verify,
            "share_x": f"https://twitter.com/intent/tweet?text={text}&url={link}",
            "share_facebook": f"https://www.facebook.com/sharer/sharer.php?u={link}",
            "share_whatsapp": f"https://wa.me/?text={text}%20{link}",
            "image": licence_image.image_url(user.licence_number, version=version),
            "download": licence_image.image_url(user.licence_number, download=True, version=version),
            "share": "https://www.linkedin.com/sharing/share-offsite/?url=" + quote(verify, safe=""),
            "next": _next_card(user),
        }
    acked = set(_paw_acked(user))
    ctx["awareness"] = [{**c, "image": card_image.image_url(f"paw-{c['id']}", org)}
                        for c in policy_awareness.approved(org) if user.licence_issued and c["id"] not in acked]
    return _render(request, user, org, "me.html", **ctx)


def _menu(user: AIDLUser, from_card: str = "") -> dict:
    """What teams_app/_menu.html needs."""
    from .avatar import can_change, start_url

    return {"avatar_url": start_url(user), "can_change_avatar": can_change(user), "from_card": from_card}


def _paw_acked(user: AIDLUser) -> list[str]:
    from .models import Acknowledgement

    return [i[4:] for i in Acknowledgement.objects.filter(user_id=str(user.pk), item__startswith="paw_")
            .values_list("item", flat=True)]


@require_POST
@_page
def me_action(request, user, org):
    """A button in the flow: what employee_flow.advance does for Slack's buttons."""
    from .employee_flow import stage
    from .licensing import acknowledge, issue_learner_if_ready

    action = request.POST.get("action", "")
    if user.role == AIDLUser.Role.ADMIN or user.slack_left_at or not admin_setup.started(org):
        return redirect("teams-app")
    if action == "start":
        if not user.slack_onboarded_at:
            user.slack_onboarded_at = timezone.now()
            user.save(update_fields=["slack_onboarded_at", "updated_at"])
        return redirect("teams-app-me")
    if action == "paw":
        policy_awareness.acknowledge(org, user, request.POST.get("id", ""), "", "")
        return redirect("teams-app-me")
    if action == "agree":
        n = int(request.POST.get("n", "0")) if request.POST.get("n", "").isdigit() else 0
        current = aup_store.required_points(org).get(n)
        if not current:
            return _flash(request, "That card isn't in use any more.", reverse("teams-app-me"))
        if request.POST.get("version") != current:
            return _flash(request, "This card was updated while you were reading. Please read the new version.",
                          reverse("teams-app-me"))
        acknowledge(user, aup_store.point_item(n), current)
    elif action in ("traffic_light", "highway_code") and card_approval.is_approved(org, action):
        acknowledge(user, action)
    else:
        return redirect("teams-app-me")
    if stage(user, org) == "licence" and not user.licence_issued:
        result = issue_learner_if_ready(user, org)
        user.refresh_from_db()
        if result == "issued":
            from .package_delivery import sync_package

            in_background(sync_package, org, user)
        elif result == "no_seats":
            _tell_no_seat(org, user)
    return redirect("teams-app-me")


def _tell_no_seat(org: Organization, member: AIDLUser) -> None:
    from .plans import user_limit
    from .slack_onboarding import primary_admin

    owner = primary_admin(org)
    if owner is not None:
        teams_channel.email(owner, "AIDL: no free licence seats",
                            f"{member.full_name or member.email} finished every card, but all {user_limit(org)} "
                            "licences on your plan are used. Their licence is waiting for a free seat.", "admin/")


@_page
def my_cards_moved(request, user, org):
    """The My cards page is gone; old links land on the person's own page."""
    return redirect("teams-app-me")


@_page
def card_page(request, user, org, no: str):
    from .package_delivery import _local_line

    teams_channel.deliver_due(org, user)
    row = PackageDelivery.objects.filter(user_id=str(user.pk), card_no=no).first()
    card = card_by_no(no)
    if card is None or row is None or row.status != PackageDelivery.Status.SENT:
        return _message(request, "This card isn't for you yet", "It appears here once it's been sent to you.",
                        user=user, org=org)
    if request.method == "POST":
        first = row.acked_at is None
        if first:
            row.acked_at = timezone.now()
            row.save(update_fields=["acked_at"])
            in_background(teams_channel.after_ack, org, user, card["title"])
        return redirect("teams-app-card", no=no)
    return _render(request, user, org, "card.html", row=row, card=card, image=card_image.image_url(f"pkg-{no}", org),
                   **_menu(user, no), local=re.sub(r"[*]", "", _local_line(org, card)) if card.get("local_link") else "")


@_page
def request_app(request, user, org):
    from_card = request.POST.get("from_card") or request.GET.get("from", "")
    title = card_by_no(from_card)["title"] if card_by_no(from_card) else ""
    ctx = {"data": [(k, v[0]) for k, v in app_requests.DATA.items()], "from_card": from_card, "card_title": title}
    if request.method != "POST":
        return _render(request, user, org, "request_app.html", **ctx)
    view = {"private_metadata": json.dumps({"from_card": title}), "state": {"values": {
        field: {"v": {"value": request.POST.get(field, "")}} for field in ("name", "website", "purpose", "data")}}}
    if not request.POST.get("purpose", "").strip():
        return _render(request, user, org, "request_app.html", **ctx, error="Tell your admin what you'd use it for.",
                       form=request.POST)
    result = app_requests.submit(user, org, view)
    if result.get("response_action") == "errors":
        return _render(request, user, org, "request_app.html", **ctx, error=" ".join(result["errors"].values()),
                       form=request.POST)
    return _flash(request, "Sent. You'll get the answer by email.", reverse("teams-app"))


# ---------- Admin Center ----------

@_admin_page
def admin_home(request, user, org):
    st = admin_setup.summary(org)
    team = licensing.team_progress(org)
    return _render(request, user, org, "admin.html", st=st, plan=usage(org), team=team, acks=card_ack.admin_stats(org),
                   can=_can(user), master=admin_perms.is_master(org, user), bar=range(st["done_count"]),
                   rest=range(5 - st["done_count"]), lost=admin_setup.state(org).get("teams_login_lost"),
                   no_team=not org.teams_team_id,
                   tz_known=admin_setup.state(org).get("tz_offset_min") is not None,
                   policy_days=[{**c, "day": _day(c.get("send_on"))} for c in st["policy"]],
                   pending_apps=AppRequest.objects.filter(organization_id=str(org.pk),
                                                          status=AppRequest.Status.PENDING).count(),
                   logo_allowed=org.plan in ("basic", "enterprise"))


def _day(iso) -> str:
    from datetime import date

    try:
        d = date.fromisoformat(iso or "")
    except (TypeError, ValueError):
        return iso or ""
    return f"{d:%a} {d.day} {d:%b}"


@require_POST
@_admin_page
def admin_timezone(request, user, org):
    try:
        minutes = int(request.POST.get("offset", ""))
    except ValueError:
        return JsonResponse({"ok": False}, status=400)
    if -840 <= minutes <= 840:
        admin_setup.update(org, tz_offset_min=minutes)
    return JsonResponse({"ok": True})


@require_POST
@_admin_page
def admin_action(request, user, org):
    """Start the programme, reminders, skipping the admins step."""
    action, can = request.POST.get("action", ""), _can(user)
    back = reverse("teams-app-admin")
    if action == "skip_admins" and can["can_admins"]:
        admin_setup.update(org, admins_step="skipped")
    elif action == "start" and can["can_send"]:
        if not card_approval.onboarding_ready(org):
            return _flash(request, "Approve the Traffic Light Check and the Highway Code first.", back)
        in_background(admin_setup.send_item, org, "flow")
        return _flash(request, "Programme started. Everyone gets the Welcome card in the channel.", back)
    elif action == "remind" and can["can_send"]:
        in_background(admin_setup.remind_item, org)
        return _flash(request, "Reminder sent.", back)
    elif action == "remind_person":
        ok = teams_channel.remind_person(org, user, request.POST.get("user_id", ""))
        return _flash(request, "Reminder sent." if ok else "They've caught up, so there's nothing to remind.", back)
    else:
        return _denied(request, user, org)
    return redirect(back)


@_admin_page
def plans_link(request, user, org):
    from .auth_jwt import create_access_token

    if not _can(user)["can_billing"]:
        return _denied(request, user, org)
    return HttpResponseRedirect(f"{settings.FRONTEND_URL.rstrip('/')}/plans?token={create_access_token(user)}")


@_admin_page
def coverage(request, user, org):
    from .slack_cards import coverage_csv, coverage_data

    response = HttpResponse(coverage_csv(coverage_data(org)["rows"]), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="aidl-coverage.csv"'
    return response


# ---------- step 1: policy cards ----------

@_admin_page
def policy(request, user, org):
    from .aup_upload import can_upload

    can = _can(user)
    if not can["can_policy"]:
        return _denied(request, user, org)
    co = aup_store.company(org)
    if request.method == "POST":
        countries = [c for c in request.POST.getlist("countries") if c in regulators.COUNTRIES]
        industry = request.POST.get("industry", "")
        if not countries or industry not in regulators.INDUSTRIES:
            return _flash(request, "Choose at least one country and your industry.",
                          reverse("teams-app-policy") + "?company=1")
        aup_store.save_company(org, countries, industry)
        return _flash(request, "Saved. Your regulators are ready to view.", reverse("teams-app-policy"))
    if not co or request.GET.get("company"):
        return _render(request, user, org, "company.html", co=co, countries=regulators.COUNTRIES,
                       industries=regulators.INDUSTRIES)
    answers = aup_store.answers(org, with_tools=False)["answers"]
    states = pc.statuses(org)
    topics = []
    for n in pc.TOPICS:
        st = states.get(n) or {"state": "new"}
        qs = len(pc.questions(n, answers)) + (1 if n == 7 else 0)
        topics.append({"n": n, "title": pc.SHORT[n], "icon": pc.ICONS[n], "state": st["state"],
                       "agreed": st.get("agreed", 0), "day": _day(st.get("send_on")), "qs": qs})
    return _render(request, user, org, "policy.html", topics=topics, done=len(states), upload_allowed=can_upload(org))


def _options(q: dict, value) -> list[dict]:
    chosen = value if isinstance(value, list) else [value]
    return [{**o, "checked": o["id"] in chosen} for o in q["options"]]


def _needs_fill(opt: dict) -> bool:
    from .policy_cards_slack import _needs_fill as needs

    return needs(opt)


def _policy_context(org: Organization, n: int, a: dict) -> dict:
    out = []
    for q in pc.questions(n, a["answers"]):
        value = a["answers"].get(q["id"])
        chosen = value if isinstance(value, list) else [value]
        fills = []
        for opt in q["options"]:
            if opt["id"] in chosen and _needs_fill(opt):
                own = opt["id"].endswith(".x") or opt["text"].lower().startswith("other")
                base = _SPECIFY.sub(" ", opt["text"]).strip()
                fills.append({"id": opt["id"], "value": a["fills"].get(opt["id"], ""),
                              "label": "Type your own" if own else f"{base} — {q['fill_label'] or 'details'}"})
        out.append({**q, "value": value if not isinstance(value, list) else "", "options": _options(q, value),
                    "fills": fills, "show_tools": q["id"] == "7.1" and value == pc.APPROVED_AI})
    tools = list(dict.fromkeys(aup_store._starting_tools(org) + pc.POPULAR_AI_TOOLS + a["tools"]))
    return {"questions": out, "tools": [{"name": t, "checked": t in a["tools"]} for t in tools],
            "preview": pc.lines(n, a)}


def _read_policy(org: Organization, post) -> tuple[dict, str, str]:
    a = aup_store.answers(org)
    answers, fills, tools = dict(a["answers"]), dict(a["fills"]), list(a["tools"])
    n = int(post["n"])
    for q in pc.questions(n, answers):
        key = f"q|{q['id']}"
        answers[q["id"]] = post.getlist(key) if q["type"] == "multi" else post.get(key, "").strip()
    for key in post:
        if key.startswith("f|"):
            fills[key[2:]] = post.get(key, "").strip()
    if "tools_shown" in post:
        tools = post.getlist("tools")
    for t in re.split(r"[\n,;]+", post.get("paste", "")):
        if t.strip() and t.strip() not in tools:
            tools.append(t.strip()[:150])
    return {"answers": answers, "fills": fills, "tools": tools}, post.get("when", "today"), post.get("day", "")


@_admin_page
def policy_card(request, user, org, n: int):
    if not _can(user)["can_policy"]:
        return _denied(request, user, org)
    if n not in pc.TOPICS:
        return redirect("teams-app-policy")
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
                return _flash(request, f"Policy card “{pc.SHORT[n]}” approved." + _when_note(org, send_on),
                              reverse("teams-app-policy"))
        aup_store.save_answers(org, a["answers"], a["fills"], a["tools"])
    existing = aup_store.card(org, n)
    return _render(request, user, org, "policy_card.html", n=n, title=pc.SHORT[n], icon=pc.ICONS[n], existing=existing,
                   errors=list(errors.values()), when=when, day=day or (timezone.localdate() + timedelta(days=1)).isoformat(),
                   **_policy_context(org, n, a))


def _when_note(org: Organization, send_on: str) -> str:
    if not admin_setup.started(org):
        return " Your team gets it when you start the programme."
    return f" It goes out {_day(send_on)} at 10:00." if send_on else " It's in your Teams channel."


@_admin_page
def regulators_page(request, user, org):
    if not _can(user)["can_policy"]:
        return _denied(request, user, org)
    co = aup_store.company(org)
    return _render(request, user, org, "regulators.html", co=co, where=", ".join(co.get("countries") or []) or "—",
                   regs=regulators.regulators_for(co.get("countries") or [], co.get("industry", "")))


@_admin_page
def upload_page(request, user, org):
    if not _can(user)["can_policy"]:
        return _denied(request, user, org)
    if not aup_upload.can_upload(org):
        return _message(request, "A paid feature", "Uploading your own AUP is part of the Basic and Enterprise plans.",
                        user=user, org=org)
    if request.method == "POST":
        upfile = request.FILES.get("file")
        if upfile is None:
            return _flash(request, "Choose your policy file.", reverse("teams-app-upload"))
        if upfile.size > settings.AUP_UPLOAD_MAX_BYTES:
            return _flash(request, "That file is too big.", reverse("teams-app-upload"))
        try:
            aup_upload.start(org, user, upfile.name, upfile.read())
        except aup_upload.UploadError as exc:
            return _flash(request, exc.message, reverse("teams-app-upload"))
        return redirect("teams-app-upload")
    upload = aup_upload.latest(org)
    if request.GET.get("new") or upload is None:
        return _render(request, user, org, "upload.html", form=True, has_logo=bool(org.logo_data))
    cards = aup_upload.cards_of(upload)
    return _render(request, user, org, "upload.html", upload=upload, cards=cards, warn=aup_upload.warnings(cards),
                   processing=upload.status == AupUpload.Status.PROCESSING,
                   failed=upload.status == AupUpload.Status.FAILED,
                   published=upload.status == AupUpload.Status.PUBLISHED)


@require_POST
@_admin_page
def upload_publish(request, user, org):
    if not _can(user)["can_policy"]:
        return _denied(request, user, org)
    upload = AupUpload.objects.filter(pk=request.POST.get("upload_id"), organization_id=str(org.pk)).first()
    back = reverse("teams-app-upload")
    if upload is None:
        return redirect(back)
    send_on = ""
    if request.POST.get("when") == "day":
        day = request.POST.get("day", "")
        why = pc.check_day(day)
        if why:
            return _flash(request, why, back)
        send_on = "" if day == timezone.localdate().isoformat() else day
    try:
        aup_upload.publish(org, upload, user, send_on)
    except aup_upload.UploadError as exc:
        return _flash(request, exc.message, back)
    return _flash(request, f"The 14 policy cards from {upload.file_name} are approved." + _when_note(org, send_on),
                  reverse("teams-app-policy"))


@_admin_page
def logo(request, user, org):
    if not _can(user)["can_admins"]:
        return _denied(request, user, org)
    back = reverse("teams-app-admin")
    if org.plan not in ("basic", "enterprise"):
        return _message(request, "A paid feature", "Your company logo on cards and licences is part of the Basic and "
                        "Enterprise plans.", user=user, org=org)
    if request.method != "POST":
        return _render(request, user, org, "logo.html")
    upfile = request.FILES.get("file")
    if upfile is None or upfile.size > LOGO_MAX_BYTES:
        return _flash(request, "Choose a PNG or JPG image under 5 MB.", reverse("teams-app-logo"))
    try:
        org.logo_data = licence_image.save_logo(upfile.read())
    except Exception:  # noqa: BLE001 — anything Pillow can't read
        return _flash(request, "AIDL couldn't read that logo file. Please upload a PNG or JPG image.",
                      reverse("teams-app-logo"))
    org.save(update_fields=["logo_data", "updated_at"])
    return _flash(request, "Your company logo is saved. It now appears on every licence and card.", back)


# ---------- step 2: admins ----------

def _admin_rows(org: Organization) -> list[dict]:
    rows = []
    for a in AIDLUser.objects.filter(organization_id=str(org.pk), role=AIDLUser.Role.ADMIN, is_active=True):
        rows.append({"id": str(a.pk), "name": a.full_name or a.email, "email": a.email,
                     "master": admin_perms.is_master(org, a), "perms": admin_perms.labels(a),
                     "keys": [k for k, f, _l, _d in admin_perms.PERMS if getattr(a, f, False)]})
    return rows


@_admin_page
def admins(request, user, org):
    from . import admin_people
    from .slack_onboarding import forget_primary_admin

    can = _can(user)
    if not can["can_admins"]:
        return _denied(request, user, org)
    back = reverse("teams-app-admins")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "remove":
            _ok, text = admin_people.remove_admin(org, user, request.POST.get("user_id", ""))
            return _flash(request, text.replace("*", ""), back)
        chosen = request.POST.getlist("perms")
        allowed = admin_perms.grantable(org, user)
        if action == "edit":
            target = AIDLUser.objects.filter(pk=request.POST.get("user_id"), organization_id=str(org.pk),
                                             role=AIDLUser.Role.ADMIN).first()
            if target is None or admin_perms.is_master(org, target):
                return _flash(request, "The master admin's permissions can't be changed.", back)
            admin_perms.apply(target, chosen, allowed)
            target.save(update_fields=[*admin_perms.FIELDS, "updated_at"])
            now = ", ".join(admin_perms.labels(target)) or "no permissions"
            in_background(admin_perms.notify_master, org, user, f"{user.full_name or user.email} changed "
                          f"{target.full_name or target.email}'s admin permissions: {now}.")
            return _flash(request, f"Saved: {target.full_name or target.email}: {now}.", back)
        return _add_admin(request, user, org, chosen, allowed, back, forget_primary_admin)
    return _render(request, user, org, "admins.html", admins=_admin_rows(org), can=can,
                   perms=[{"key": k, "label": l, "desc": d} for k, _f, l, d in admin_perms.PERMS if k in admin_perms.grantable(org, user)],
                   seats=len(_admin_rows(org)), limit=org.admin_seat_limit)


def _add_admin(request, user, org, chosen, allowed, back, forget_primary_admin):
    from .microsoft_auth import add_team_member, resolve_user_by_email

    email = request.POST.get("email", "").strip()
    if not email:
        return _flash(request, "Enter their work email.", back)
    if len(_admin_rows(org)) >= org.admin_seat_limit:
        return _flash(request, f"All {org.admin_seat_limit} admin seats are used.", back)
    token = teams_channel.token_for(org)
    found = resolve_user_by_email(token, email) if token else None
    if not found:
        return _flash(request, "This person isn't in your company's Microsoft 365 directory.", back)
    person = {"user_id": found["id"], "email": found.get("mail") or found.get("userPrincipalName") or email,
              "full_name": found.get("displayName", "")}
    target = AIDLUser.objects.filter(microsoft_id=person["user_id"]).first()
    if target is not None and target.organization_id not in ("", str(org.pk)):
        return _flash(request, "This person already belongs to another AIDL organization.", back)
    if target is None:
        target = teams_channel._new_member(org, person)  # noqa: SLF001
    target.organization_id, target.organization_name = str(org.pk), org.name
    target.role, target.slack_left_at = AIDLUser.Role.ADMIN, None
    admin_perms.apply(target, [], [k for k, *_ in admin_perms.PERMS])
    admin_perms.apply(target, chosen, allowed)
    target.save()
    forget_primary_admin(org)
    add_team_member(token, team_id=org.teams_team_id, aad_user_id=person["user_id"])
    teams_channel.email(target, "You're an AIDL admin", f"{user.full_name or user.email} added you as an AIDL admin for "
                        f"{org.name}. Sign in with your Microsoft account to open the Admin Center.", "admin/")
    in_background(admin_perms.notify_master, org, user, f"{user.full_name or user.email} added "
                  f"{target.full_name or target.email} as an admin "
                  f"({', '.join(admin_perms.labels(target)) or 'no permissions'}).")
    return _flash(request, f"Invited {target.email or target.full_name}.", back)


# ---------- step 3: team ----------

@_admin_page
def team(request, user, org):
    from .microsoft_auth import add_team_member, directory_users

    can = _can(user)
    back = reverse("teams-app-team")
    if request.method == "POST":
        if request.POST.get("action") == "remove":
            _ok, text = teams_channel.remove_member(org, user, request.POST.get("user_id", ""))
            return _flash(request, text, back)
        if not can["can_team"]:
            return _denied(request, user, org)
        token = teams_channel.token_for(org)
        people = directory_users(token) if token else None
        if people is None:
            return _flash(request, "AIDL couldn't reach Microsoft. Please sign in again and retry.", back)
        known = _known_ids(org)
        eligible = {p["user_id"]: p for p in people if p["user_id"] not in known}
        picked = list(eligible) if request.POST.get("everyone") else [i for i in request.POST.getlist("people") if i in eligible]
        if not picked:
            return _flash(request, "Pick at least one person.", back)
        u = usage(org)
        if len(picked) > u["free"]:
            return _flash(request, f"{len(picked)} selected, but only {u['free']} seats left on your {u['label']} plan "
                                   f"({u['used']} of {u['limit']} used). Remove some people or upgrade.", back)

        def add_all():
            for uid in picked:
                add_team_member(token, team_id=org.teams_team_id, aad_user_id=uid)
                teams_channel.join(org, eligible[uid], token)

        in_background(add_all)
        return _flash(request, f"Adding {len(picked)} {'person' if len(picked) == 1 else 'people'} to the AIDL team. "
                               "They'll get their welcome in a moment.", back)
    token = teams_channel.token_for(org)
    people = (directory_users(token) if token else None) if can["can_team"] else []
    known = _known_ids(org)
    acks = card_ack.member_stats(org)
    rows = licensing.team_progress(org)
    for r in rows["rows"]:
        r["id"] = str(r["member"].pk)
        r["label"] = licensing.STATUS_LABELS.get(r["status"], r["status"])
        r["acks"] = acks.get(r["id"])
    return _render(request, user, org, "team.html", progress=rows, plan=usage(org), can=can, directory_failed=people is None,
                   people=[p for p in (people or []) if p["user_id"] not in known])


def _known_ids(org: Organization) -> set[str]:
    """Already in the programme (someone who left can be added back)."""
    return set(AIDLUser.objects.filter(organization_id=str(org.pk), is_active=True, slack_left_at__isnull=True)
               .values_list("microsoft_id", flat=True))


# ---------- step 4: card approval ----------

@_admin_page
def cards(request, user, org):
    can = _can(user)
    full = can["can_approve_cards"]
    assigned = set(card_approval.assigned_keys(org, user))
    if not full and not assigned:
        return _message(request, "Approving cards needs the Approve cards permission", "Ask your master admin.",
                        user=user, org=org)
    month = request.GET.get("month", "1")
    month = min(max(1, int(month) if month.isdigit() else 1), card_approval.months(org))
    names = card_approval.assignee_names(org)
    rows = card_approval.month_cards(org, month) if full else [
        c for c in card_approval.sequence(org) if f"pkg:{c['no']}" in assigned]
    acks = card_ack.admin_stats(org)
    for c in rows:
        c["assignee"] = names.get(f"pkg:{c['no']}", "") if c["approval"] == card_approval.WAITING else ""
        c["rate"] = acks["card_rates"].get(c["no"])
        c["image"] = card_image.image_url(f"pkg-{c['no']}", org)
    paw = card_approval.policy_awareness(org)
    for c in paw:
        c["image"] = card_image.image_url(f"paw-{c['id']}", org)
        c["assignee"] = names.get(f"paw:{c['id']}", "") if c["status"] != card_approval.APPROVED else ""
    onboarding = card_approval.onboarding(org)
    for r in onboarding:
        r["image"] = card_image.image_url(r["item"], org)
    policy = pc.summary(org)
    sent = sum(c["state"] == "sent" for c in policy)
    other_admins = [a for a in _admin_rows(org) if a["id"] != str(user.pk)]
    return _render(request, user, org, "cards.html", full=full, month=month, months=card_approval.months(org),
                   prev=month - 1, next=month + 1 if month < card_approval.months(org) else 0, cards=rows, paw=paw if full else
                   [c for c in paw if f"paw:{c['id']}" in assigned], onboarding=onboarding, counts=card_approval.counts(org),
                   policy_sent=sent, policy_later=len(policy) - sent, admins=other_admins, can=can,
                   waiting_here=sum(c["approval"] == card_approval.WAITING for c in rows), acks=acks)


@require_POST
@_admin_page
def cards_action(request, user, org):
    from .policy_awareness import by_id

    can = _can(user)
    full = can["can_approve_cards"]
    action, value = request.POST.get("action", ""), request.POST.get("value", "")
    month = request.POST.get("month", "1")
    back = f"{reverse('teams-app-cards')}?month={month if month.isdigit() else 1}"
    key = f"paw:{value}" if action == "approve_paw" else f"pkg:{value}"
    assigned_ok = action in ("approve", "skip", "approve_paw") and card_approval.is_assigned(org, key, user)
    if not full and not assigned_ok:
        return _denied(request, user, org)
    notice, summary = "Saved.", ""
    if action == "approve_onboarding" and value in card_approval.ONBOARDING:
        card_approval.approve_onboarding(org, value, user)
        notice, summary = "Approved.", f"approved {card_approval.ONBOARDING_LABEL[value]}."
    elif action == "approve_paw" and by_id(value) is not None:
        card_approval.approve_policy_awareness(org, value, user)
        in_background(teams_channel.post_policy_awareness, org)
        notice, summary = "Approved.", f"approved the awareness card {by_id(value)['title']}."
    elif action == "approve":
        n = card_approval.approve(org, [value], user)
        notice, summary = "Approved.", f"approved {n} awareness card."
    elif action == "approve_month" and month.isdigit():
        n = card_approval.approve_waiting(org, user, int(month))
        notice, summary = f"Approved {n} cards.", f"approved {n} awareness cards."
    elif action == "approve_all":
        n = card_approval.approve_waiting(org, user)
        notice, summary = f"Approved {n} cards.", f"approved {n} awareness cards."
    elif action == "skip":
        card_approval.skip(org, value, user)
        notice, summary = "Skipped. It won't be sent.", f"skipped the card {value}."
    elif action == "unskip":
        card_approval.unskip(org, value, user)
        notice = "It's waiting for approval again."
    elif action == "swap":
        new = card_approval.swap(org, value, user)
        notice = f"Swapped for “{new['title']}”, which now needs approving." if new else "That topic has no cards left to swap in."
    elif action == "assign":
        return _assign(request, user, org, back)
    else:
        return redirect(back)
    in_background(card_approval.after_change, org, user, summary)
    return _flash(request, notice, back)


def _assign(request, user, org, back):
    to = AIDLUser.objects.filter(pk=request.POST.get("to"), organization_id=str(org.pk), role=AIDLUser.Role.ADMIN,
                                 is_active=True).first()
    if to is None:
        return _flash(request, "Pick an admin.", back)
    value = request.POST.get("value", "")
    if value.startswith("month:") and value[6:].isdigit():
        keys = [f"pkg:{c['no']}" for c in card_approval.month_cards(org, int(value[6:]))
                if c["approval"] == card_approval.WAITING]
    elif value.startswith(("pkg:", "paw:")):
        keys = [value]
    else:
        return redirect(back)
    n = card_approval.assign(org, keys, to, user)
    teams_channel.email(to, "AIDL cards to review", f"{user.full_name or user.email} asked you to review {n} AIDL "
                        f"card{'s' if n != 1 else ''}.", "admin/cards/")
    return _flash(request, f"Asked {to.full_name or to.email} to review {n} card{'s' if n != 1 else ''}.", back)


# ---------- apps ----------

@_admin_page
def apps(request, user, org):
    from .admin_ops import AdminOpsError, add_registered_app
    from .slack_cards import AI_CATEGORIES, DATA_ALLOWED, IT_CATEGORIES

    can = _can(user)
    if not can["can_apps"]:
        return _denied(request, user, org)
    kind = "it" if request.GET.get("kind") == "it" or request.POST.get("kind") == "it" else "ai"
    back = f"{reverse('teams-app-apps')}?kind={kind}"
    if request.method == "POST":
        from .slack_modals import _DATA_VALUE

        status = RegisteredApp.Status.APPROVED if request.POST.get("status") == "Approved" else RegisteredApp.Status.REJECTED
        try:
            add_registered_app(org, app_type=RegisteredApp.AppType.AI if kind == "ai" else RegisteredApp.AppType.IT,
                               name=request.POST.get("name", "").strip(), category=request.POST.get("category") or "Other",
                               data_allowed=_DATA_VALUE.get(request.POST.get("data", ""), ""), status=status)
        except AdminOpsError as exc:
            return _flash(request, exc.message or exc.code, back)
        return _flash(request, "Added.", back)
    rows = RegisteredApp.objects.filter(organization_id=str(org.pk), is_active=True,
                                        app_type=RegisteredApp.AppType.AI if kind == "ai" else RegisteredApp.AppType.IT).order_by("name")
    requests_ = []
    for r in AppRequest.objects.filter(organization_id=str(org.pk), status=AppRequest.Status.PENDING):
        people = app_requests._requesters(r)  # noqa: SLF001
        requests_.append({"row": r, "names": ", ".join(p.full_name or p.email for p in people) or "an employee",
                          "data": app_requests.DATA.get(r.data_class, ("", ""))[0]})
    return _render(request, user, org, "apps.html", kind=kind, apps=rows, requests=requests_, data=[d for d, _c in DATA_ALLOWED],
                   categories=AI_CATEGORIES if kind == "ai" else IT_CATEGORIES)


@require_POST
@_admin_page
def app_decision(request, user, org):
    if not _can(user)["can_apps"]:
        return _denied(request, user, org)
    back = reverse("teams-app-apps")
    req = app_requests.get(org, request.POST.get("request_id", ""))
    if req is None:
        return _flash(request, "This request no longer exists.", back)
    action, text = request.POST.get("action"), request.POST.get("text", "").strip()[:1000]
    if action == "approve":
        app_requests.approve(org, req, user)
        return _flash(request, f"{req.name} approved.", back)
    if action == "reject":
        app_requests.reject(org, req, user, text)
        return _flash(request, f"{req.name} rejected.", back)
    if action == "info":
        if not text:
            return _flash(request, "Type your question.", back)
        app_requests.ask_info(org, req, user, text)
        return _flash(request, "Question sent.", back)
    return redirect(back)


@_admin_page
def alerts(request, user, org):
    p = card_ack.prefs_for(org, user)
    live = {"none": card_ack.LIVE["none"][0], "every": "An email for every Got it"}
    late = {"alert": "Overdue alert by email (at most one a day)", "weekly": card_ack.LATE["weekly"][0]}
    if request.method == "POST":
        new_live, new_late = request.POST.get("live"), request.POST.get("late")
        if new_live not in live or new_late not in late:
            return _flash(request, "Choose an option.", reverse("teams-app-alerts"))
        saved = dict(admin_setup.state(org).get("ack_alerts") or {})
        saved[str(user.pk)] = {"live": new_live, "late": new_late}
        admin_setup.update(org, ack_alerts=saved)
        return _flash(request, "Saved.", reverse("teams-app-admin"))
    return _render(request, user, org, "alerts.html", live=live.items(), late=late.items(),
                   cur_live=p["live"] if p["live"] in live else "none", cur_late=p["late"])


# ---------- the hourly job, from outside ----------

@csrf_exempt
@require_POST
def dispatch_view(request):
    """For a scheduler that can only call a URL. Off until TEAMS_CRON_SECRET is set."""
    import hmac

    secret = settings.TEAMS_CRON_SECRET
    sent = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not secret or not hmac.compare_digest(sent, secret):
        return JsonResponse({"detail": "Not allowed."}, status=403)
    in_background(teams_channel.dispatch_all)
    return JsonResponse({"ok": True})
