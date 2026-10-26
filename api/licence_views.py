"""GET /api/licenses/<number>.png?s=<signature>[&download=1]
GET /api/cards/<kind>/<org id>.png?s=<signature>&v=<logo version>
GET /api/aup/banner.png?l=&r=&o=&at=&n=&v=&s=<signature>  (AI policy builder strip, aup_banner.py)

The licence image Slack shows on the licence card (licence_image.py) and the
Highway Code / Traffic Light Check card images (card_image.py). The
signatures stop people fetching other licences or companies' cards by
guessing ids."""

from __future__ import annotations

import hmac

from django.http import Http404, HttpResponse
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
    params = {k: request.GET.get(k, "") for k in ("l", "r", "o", "at", "n", "v")}
    if (not params["at"].isdigit() or not params["n"].isdigit()
            or not hmac.compare_digest(request.GET.get("s", ""), aup_banner.signature(params))):
        raise Http404
    resp = HttpResponse(aup_banner.png_for(params), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp
