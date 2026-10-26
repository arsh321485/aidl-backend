"""AUP question bank + generator preview (aup_builder.py).

Stateless: nothing is saved yet. Used to try the generator and, later, by
the Slack AI policy builder screens.

    GET  /api/aup/questions/  sections, questions, options, recommended answers
    POST /api/aup/preview/    {answers, fills} → the 14 generated points
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import aup_builder


@extend_schema(summary="AUP question bank (15 sections, recommended answers pre-selected)",
               responses={200: OpenApiResponse(OpenApiTypes.OBJECT)})
@api_view(["GET"])
@permission_classes([AllowAny])
def questions(request):
    bank = aup_builder.bank()
    return Response({
        "source": bank["source"],
        "sections": bank["sections"],
        "questions": [{k: v for k, v in q.items() if k != "option_by_id"} for q in bank["questions"]],
        "recommended_answers": aup_builder.recommended_answers(),
    })


@extend_schema(
    summary="Generate an AUP from questionnaire answers (preview, not saved)",
    request=OpenApiTypes.OBJECT,
    responses={200: OpenApiResponse(OpenApiTypes.OBJECT, description="{aup, text}"),
               400: OpenApiResponse(OpenApiTypes.OBJECT, description='{"errors": {"<question id>": "..."}}')},
)
@api_view(["POST"])
@permission_classes([AllowAny])
def preview(request):
    """Body: {"answers": {...}, "fills": {...}, "use_recommended": true}.
    use_recommended starts from the pre-selected answers, so only changes
    and typed details need sending."""
    body = request.data if isinstance(request.data, dict) else {}
    answers = dict(aup_builder.recommended_answers()) if body.get("use_recommended") else {}
    answers.update(body.get("answers") or {})
    fills = body.get("fills") or {}
    try:
        aup = aup_builder.generate(answers, fills)
    except aup_builder.AupAnswersError as exc:
        return Response({"errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"aup": aup, "text": aup_builder.as_text(aup)})
