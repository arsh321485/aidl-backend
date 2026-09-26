"""Swagger / OpenAPI (drf-spectacular) support.

* ``AIDLAutoSchema`` — groups endpoints into tags by URL and documents the
  function views that have no serializer as generic JSON objects, so every
  route shows up in Swagger UI instead of being skipped.
* ``JWTAuthenticationScheme`` — lets the "Authorize" button send JWTs.
* ``*Doc`` serializers — response shapes used only for the docs.
"""

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers
from rest_framework.generics import GenericAPIView

from .serializers import AIDLUserSerializer

# Longest prefix first — the first match wins.
_TAGS = [
    ("/api/auth/teams/", "Teams auth"),
    ("/api/admin/users/microsoft-teams/", "Teams auth"),
    ("/api/auth/slack/", "Slack auth"),
    ("/api/auth/", "Auth"),
    ("/api/locations/", "Locations"),
    ("/api/org/", "Organization"),
    ("/api/slack/", "Slack app"),
    ("/api/teams/admin/", "Teams admin"),
    ("/api/teams/bot/", "Teams bot"),
    ("/api/teams/", "Teams app"),
    ("/api/items/", "Items"),
]


class AIDLAutoSchema(AutoSchema):
    def get_tags(self):
        for prefix, tag in _TAGS:
            if self.path.startswith(prefix):
                return [tag]
        return ["System"]

    def _has_no_serializer(self) -> bool:
        return not isinstance(self.view, GenericAPIView) and not hasattr(
            self.view, "serializer_class"
        )

    def get_request_serializer(self):
        if self._has_no_serializer():
            return OpenApiTypes.OBJECT if self.method in ("POST", "PUT", "PATCH") else None
        return super().get_request_serializer()

    def get_response_serializers(self):
        if self._has_no_serializer():
            return OpenApiTypes.OBJECT
        return super().get_response_serializers()


class JWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "api.auth_views.JWTAuthentication"
    name = "BearerAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "access_token from /api/auth/signup/ or /api/auth/signin/",
        }


class DetailDoc(serializers.Serializer):
    detail = serializers.CharField()


class ErrorDoc(serializers.Serializer):
    error = serializers.CharField()


class MessageDoc(serializers.Serializer):
    message = serializers.CharField()


class TokenPairDoc(serializers.Serializer):
    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    token_type = serializers.CharField(default="Bearer")


class AuthResponseDoc(TokenPairDoc):
    message = serializers.CharField()
    user = AIDLUserSerializer()


class RefreshRequestDoc(serializers.Serializer):
    refresh_token = serializers.CharField()


class MeDoc(AIDLUserSerializer):
    teams_url = serializers.CharField()
    teams_platform_url = serializers.CharField()
    teams_channel_url = serializers.CharField(allow_null=True)
    landed_on = serializers.ChoiceField(choices=["home_tab", "channel", "chat"])
    teams_connected = serializers.BooleanField()

    class Meta(AIDLUserSerializer.Meta):
        fields = AIDLUserSerializer.Meta.fields + [
            "teams_url",
            "teams_platform_url",
            "teams_channel_url",
            "landed_on",
            "teams_connected",
        ]
        read_only_fields = fields


class CountryDoc(serializers.Serializer):
    code = serializers.CharField(help_text="ISO 3166-1 alpha-2, e.g. US")
    name = serializers.CharField()
    phone_code = serializers.CharField(help_text="e.g. +1")
    flag = serializers.CharField(help_text="Flag emoji")


class StateDoc(serializers.Serializer):
    code = serializers.CharField(help_text="e.g. CA")
    name = serializers.CharField()


class CityDoc(serializers.Serializer):
    name = serializers.CharField()


class CountryListDoc(serializers.Serializer):
    count = serializers.IntegerField()
    results = CountryDoc(many=True)


class StateListDoc(serializers.Serializer):
    country = CountryDoc()
    count = serializers.IntegerField()
    results = StateDoc(many=True)


class CityListDoc(serializers.Serializer):
    country = CountryDoc()
    state = StateDoc()
    count = serializers.IntegerField()
    results = CityDoc(many=True)
