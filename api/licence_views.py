"""GET /api/licenses/<number>.png?s=<signature>[&download=1]

The licence image Slack shows on the licence card (licence_image.py). The
signature stops people fetching other licences by guessing numbers."""

from __future__ import annotations

import hmac

from django.http import Http404, HttpResponse
from django.views.decorators.http import require_GET

from . import licence_image
from .models import AIDLUser
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
