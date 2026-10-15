"""GET /api/licenses/<number>.png?s=<signature>[&download=1]
GET /api/cards/<kind>/<org id>.png?s=<signature>&v=<logo version>

The licence image Slack shows on the licence card (licence_image.py) and the
Highway Code / Traffic Light Check card images (card_image.py). The
signatures stop people fetching other licences or companies' cards by
guessing ids."""

from __future__ import annotations

import hmac

from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET

from . import card_image, licence_image
from .models import AIDLUser, Organization
from .org_service import get_organization_for_user


@require_GET
def licence_png(request, number: str):
    if not hmac.compare_digest(request.GET.get("s", ""), licence_image.signature(number)):
        raise Http404
    member = AIDLUser.objects.filter(licence_number=number, licence_issued=True).first()
    if member is None:
        raise Http404
    org = get_organization_for_user(member)
    png = licence_image.render(name=member.full_name or member.email, org_name=org.name if org else "",
                               number=number, issued=member.licence_issued_at, org=org,
                               avatar_url=member.avatar_url or "")
    resp = HttpResponse(png, content_type="image/png")
    resp["Cache-Control"] = "public, max-age=3600"
    if request.GET.get("download"):
        resp["Content-Disposition"] = f'attachment; filename="{number}.png"'
    return resp


@require_GET
def card_png(request, kind: str, org_id: str):
    if kind not in card_image.KINDS or not hmac.compare_digest(request.GET.get("s", ""),
                                                               card_image.signature(kind, org_id)):
        raise Http404
    org = Organization.objects.filter(pk=org_id).first()
    if org is None:
        raise Http404
    resp = HttpResponse(card_image.render(kind, org), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=86400"  # the URL changes with the logo
    return resp
