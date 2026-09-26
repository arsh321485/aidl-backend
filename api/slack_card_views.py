"""Slack Admin cards + User cards pages (AIDL Slack Handover Guide, sections
8 and 10).

Identity comes from an AIDL access JWT — `?token=` (for links opened from
Slack) or `Authorization: Bearer`. With no token the pages render the
guide's demo data as a design preview.
"""

import csv

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from .auth_jwt import decode_token
from .models import AIDLUser, CardRequest, TrafficLightRating
from .org_service import compute_org_metrics, get_organization_for_user
from .slack_cards import build_admin_cards_context, build_user_cards_context


class _BadToken(Exception):
    pass


def _user_from_request(request) -> AIDLUser | None:
    token = (request.GET.get("token") or "").strip()
    header = request.headers.get("Authorization", "")
    if not token and header.startswith("Bearer "):
        token = header.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise _BadToken
        return AIDLUser.objects.get(pk=payload["sub"], is_active=True)
    except Exception as exc:  # noqa: BLE001
        raise _BadToken from exc


def _error_page(request, title: str, message: str, status: int):
    return render(
        request,
        "slack/message.html",
        {"title": title, "message": message},
        status=status,
    )


@never_cache
def slack_admin_cards(request):
    try:
        user = _user_from_request(request)
    except _BadToken:
        return _error_page(request, "Link expired", "Open the AIDL Admin Center again from Slack.", 401)
    if user is not None and user.role != AIDLUser.Role.ADMIN:
        return _error_page(request, "Admins only", "The Admin Center is only available to AIDL admins.", 403)

    if user is not None and get_organization_for_user(user) is None:
        return _error_page(
            request,
            "Organization not set up yet",
            "Sign in with Slack as an Organization on the AIDL website to create your Admin Center.",
            404,
        )

    data = build_admin_cards_context(user)
    return render(request, "slack/admin_cards.html", {"d": data})


@never_cache
def slack_user_cards(request):
    try:
        user = _user_from_request(request)
    except _BadToken:
        return _error_page(request, "Link expired", "Open AIDL again from Slack.", 401)

    lights = [l for l in request.GET.get("lights", "").split(",") if l]
    data = build_user_cards_context(user, lights)
    rating = TrafficLightRating.objects.first()
    data["likes"] = rating.likes if rating else 128
    data["dislikes"] = rating.dislikes if rating else 6
    data["user_key"] = str(user.pk) if user else "demo"
    data["download_email"] = user.email if user else ""
    return render(request, "slack/user_cards.html", {"d": data})


@never_cache
def slack_admin_coverage_csv(request):
    """Guide 8.1 — Export Coverage CSV: every user with license class, AUP
    status and how many reference cards the organization has sent."""
    try:
        user = _user_from_request(request)
    except _BadToken:
        user = None
    if user is None or user.role != AIDLUser.Role.ADMIN:
        return HttpResponse("Admin sign-in required.", status=401, content_type="text/plain")
    org = get_organization_for_user(user)
    if org is None:
        return HttpResponse("No organization found for this admin.", status=404, content_type="text/plain")

    metrics = compute_org_metrics(org)
    cards_sent = CardRequest.objects.filter(
        organization_id=str(org.pk), status=CardRequest.Status.SENT
    ).count()
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="aidl-coverage.csv"'
    writer = csv.writer(response)
    writer.writerow(["name", "email", "role", "license_class", "license_number", "aup_signed", "cards_received"])
    for m in metrics["members"]:
        writer.writerow(
            [
                m.full_name,
                m.email,
                m.role,
                "L" if m.licence_issued else "",
                m.licence_number,
                "yes" if m.aup_signed else "no",
                cards_sent,
            ]
        )
    return response
