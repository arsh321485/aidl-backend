"""GET /api/licenses/<number>.png?s=<signature>[&download=1]
GET /api/cards/<kind>/<org id>.png?s=<signature>&v=<logo version>
GET /api/aup/banner.png?l=&r=&o=&at=&n=&v=&s=<signature>  (AI policy builder strip, aup_banner.py)

The licence image Slack shows on the licence card (licence_image.py) and the
Highway Code / Traffic Light Check card images (card_image.py). The
signatures stop people fetching other licences or companies' cards by
guessing ids."""

from __future__ import annotations

import hmac
import re

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import aup_banner, card_image, licence_image
from .models import AIDLUser, Organization
from .org_service import get_organization_for_user


@require_GET
def licence_png(request, number: str):
    if not hmac.compare_digest(request.GET.get("s", ""), licence_image.signature(number)):
        raise Http404
    member = AIDLUser.objects.filter(licence_number=number, licence_issued=True).first()
    if member is None:  # revoked / reset: old Slack messages still show something sensible
        resp = HttpResponse(licence_image.render_void(number), content_type="image/png")
        resp["Cache-Control"] = "no-cache"
        return resp
    org = get_organization_for_user(member)
    resp = HttpResponse(licence_image.png_for(member, org), content_type="image/png")  # the chosen avatar only
    resp["Cache-Control"] = "public, max-age=3600"
    if request.GET.get("download"):
        resp["Content-Disposition"] = f'attachment; filename="{number}.png"'
    return resp


_NUMBER = re.compile(r"^AIDL-L-\d{4}-\d{4}$")


def _issued_member(number: str) -> AIDLUser | None:
    if not _NUMBER.match(number or ""):
        return None
    return AIDLUser.objects.filter(licence_number=number, licence_issued=True).first()


def short_name(full_name: str) -> str:
    """"Jordan Ellis" → "Jordan E." — what the public check shows for a licence nobody shared."""
    parts = (full_name or "").split()
    if len(parts) < 2:
        return parts[0] if parts else "Licence holder"
    return f"{parts[0]} {parts[-1][0]}."


def _facts(member: AIDLUser) -> dict:
    from .licensing import licence_status

    org = get_organization_for_user(member)
    day = lambda d: d.strftime("%d %b %Y") if d else "—"  # noqa: E731
    return {"org": (org.name if org else "") or member.organization_name, "issued": day(member.licence_issued_at),
            "expires": day(member.licence_expires_at), "status": licence_status(member).lower(), "org_obj": org}


@require_GET
def licence_verify(request):
    """GET /api/licenses/verify/?lic=AIDL-L-… — the website's "Verify a licence" box. Public, so the name is
    shortened. { found: false } only when there is no such licence."""
    member = _issued_member((request.GET.get("lic") or "").strip().upper())
    if member is None:
        return JsonResponse({"found": False})
    facts = _facts(member)
    return JsonResponse({"found": True, "name": short_name(member.full_name), "org": facts["org"],
                         "issued": member.licence_issued_at.date().isoformat() if member.licence_issued_at else "",
                         "expires": member.licence_expires_at.date().isoformat() if member.licence_expires_at else "",
                         "status": facts["status"]})


@require_GET
def licence_page(request, number: str):
    """GET /api/licenses/<number>/[?s=<signature>] — the page behind every Share button. With the owner's
    signature it carries the licence picture and a link preview (Open Graph) so a post on LinkedIn, X,
    Facebook or WhatsApp shows the licence; without it, it only confirms the licence."""
    member = _issued_member(number)
    if member is None:
        resp = render(request, "licence/public.html", {"found": False, "number": number}, status=404)
        resp["X-Robots-Tag"] = "noindex"
        return resp
    facts = _facts(member)
    owner = hmac.compare_digest(request.GET.get("s", ""), licence_image.signature(number))
    name = member.full_name if owner else short_name(member.full_name)
    ctx = {"found": True, "number": number, "name": name, "owner": owner, "org": facts["org"],
           "issued": facts["issued"], "expires": facts["expires"], "status": facts["status"],
           "home": settings.FRONTEND_URL.rstrip("/"),
           "page_url": request.build_absolute_uri(request.path) + (f"?s={request.GET['s']}" if owner else "")}
    if owner:
        org = facts["org_obj"]
        ctx["image"] = licence_image.image_url(number, version=licence_image.url_version(member, org))
    resp = render(request, "licence/public.html", ctx)
    resp["Cache-Control"] = "public, max-age=300"
    return resp


@require_GET
def card_png(request, kind: str, org_id: str):
    if not card_image.valid_kind(kind) or not hmac.compare_digest(request.GET.get("s", ""),
                                                               card_image.signature(kind, org_id)):
        raise Http404
    org = Organization.objects.filter(pk=org_id).first()
    if org is None:
        raise Http404
    resp = HttpResponse(card_image.png_for(kind, org), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"  # the URL changes with the picture
    return resp


@require_GET
def aup_banner_png(request):
    params = {k: request.GET.get(k, "") for k in aup_banner.FIELDS if k != "d"}
    if "d" in request.GET:  # positions already done (policy cards)
        params["d"] = request.GET["d"]
    if (not params["at"].isdigit() or not params["n"].isdigit()
            or not hmac.compare_digest(request.GET.get("s", ""), aup_banner.signature(params))):
        raise Http404
    resp = HttpResponse(aup_banner.png_for(params), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp
