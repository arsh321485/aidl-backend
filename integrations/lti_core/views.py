from django.http import JsonResponse

from .keys import public_jwks


def jwks(request):
    """GET /api/lti/jwks/ — AIDL's public keys (the "Keyset URL" in the LMS)."""
    return JsonResponse(public_jwks())
