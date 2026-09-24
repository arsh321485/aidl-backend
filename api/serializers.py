import re
import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import AIDLUser, Item

_NAME_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ \-'.])*$", re.UNICODE)
_MOBILE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{6,19}$")
_LICENSE_ALIASES = {
    "class_l": AIDLUser.LicenseClass.CLASS_L,
    "class l": AIDLUser.LicenseClass.CLASS_L,
    "class-l": AIDLUser.LicenseClass.CLASS_L,
    "l": AIDLUser.LicenseClass.CLASS_L,
    "learner's permit": AIDLUser.LicenseClass.CLASS_L,
    "learners permit": AIDLUser.LicenseClass.CLASS_L,
    "class l · learner's permit": AIDLUser.LicenseClass.CLASS_L,
}


class ItemSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = Item
        fields = ["id", "name", "description", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class AIDLUserSerializer(serializers.ModelSerializer):
    id = serializers.CharField(read_only=True)

    class Meta:
        model = AIDLUser
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "mobile_number",
            "country",
            "state",
            "city",
            "license_class",
            "enroll_as",
            "role",
            "organization_id",
            "organization_name",
            "provider",
            "microsoft_id",
            "avatar_url",
            "licence_issued",
            "aup_signed",
            "aup_signed_at",
            "teams_team_id",
            "teams_channel_id",
            "teams_channel_name",
            "is_active",
            "last_login_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


def _normalize_license_class(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in AIDLUser.LicenseClass.values:
        return raw
    if raw in _LICENSE_ALIASES:
        return _LICENSE_ALIASES[raw]
    return ""


class SignupSerializer(serializers.Serializer):
    """Website registration — matches the Individual / Organization form."""

    enroll_as = serializers.ChoiceField(
        choices=AIDLUser.EnrollAs.choices,
        default=AIDLUser.EnrollAs.INDIVIDUAL,
    )
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    confirm_password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    mobile_number = serializers.CharField(max_length=32)
    country = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=100)
    city = serializers.CharField(max_length=100)
    license_class = serializers.CharField(
        max_length=64,
        required=False,
        default=AIDLUser.LicenseClass.CLASS_L,
    )
    organization_name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )

    def validate_first_name(self, value: str) -> str:
        value = (value or "").strip()
        if not value or not _NAME_RE.fullmatch(value):
            raise serializers.ValidationError("Enter a valid first name.")
        return value

    def validate_last_name(self, value: str) -> str:
        value = (value or "").strip()
        if not value or not _NAME_RE.fullmatch(value):
            raise serializers.ValidationError("Enter a valid last name.")
        return value

    def validate_email(self, value: str) -> str:
        email = (value or "").strip().lower()
        if AIDLUser.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email

    def validate_mobile_number(self, value: str) -> str:
        mobile = re.sub(r"\s+", " ", (value or "").strip())
        if not _MOBILE_RE.fullmatch(mobile):
            raise serializers.ValidationError(
                "Enter a valid mobile number with country code, e.g. +15550000000."
            )
        return mobile

    def validate_country(self, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("Country is required.")
        return value

    def validate_state(self, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("State is required.")
        return value

    def validate_city(self, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("City is required.")
        return value

    def validate_license_class(self, value: str) -> str:
        normalized = _normalize_license_class(value)
        if not normalized:
            raise serializers.ValidationError(
                "Invalid license class. Use class_l (Class L · Learner's Permit)."
            )
        return normalized

    def validate_organization_name(self, value: str) -> str:
        return (value or "").strip()

    def validate(self, attrs):
        password = attrs.get("password") or ""
        if password != (attrs.get("confirm_password") or ""):
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )

        dummy = AIDLUser(
            email=attrs.get("email", ""),
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
            full_name=f"{attrs.get('first_name', '')} {attrs.get('last_name', '')}".strip(),
        )
        try:
            validate_password(password, user=dummy)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc

        enroll_as = attrs.get("enroll_as") or AIDLUser.EnrollAs.INDIVIDUAL
        org_name = attrs.get("organization_name") or ""
        if enroll_as == AIDLUser.EnrollAs.ORGANIZATION and not org_name:
            raise serializers.ValidationError(
                {"organization_name": "Organization name is required for organization signup."}
            )
        if enroll_as != AIDLUser.EnrollAs.ORGANIZATION:
            attrs["organization_name"] = ""
        return attrs

    def create(self, validated_data):
        validated_data.pop("confirm_password", None)
        password = validated_data.pop("password")
        first_name = validated_data["first_name"]
        last_name = validated_data["last_name"]
        enroll_as = validated_data.get("enroll_as") or AIDLUser.EnrollAs.INDIVIDUAL
        role = (
            AIDLUser.Role.ADMIN
            if enroll_as == AIDLUser.EnrollAs.ORGANIZATION
            else AIDLUser.Role.LEARNER
        )
        user = AIDLUser(
            microsoft_id=f"local:{uuid.uuid4()}",
            provider="website",
            full_name=f"{first_name} {last_name}".strip(),
            role=role,
            is_active=True,
            **validated_data,
        )
        user.set_password(password)
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    """Website sign-in — email, password, and Individual / Organization tab."""

    enroll_as = serializers.ChoiceField(choices=AIDLUser.EnrollAs.choices)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, max_length=128)

    def validate(self, attrs):
        email = (attrs.get("email") or "").strip().lower()
        password = attrs.get("password") or ""
        enroll_as = attrs.get("enroll_as") or AIDLUser.EnrollAs.INDIVIDUAL
        user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            raise serializers.ValidationError(
                {"email": "No account found with this email."}
            )
        if user.enroll_as != enroll_as:
            raise serializers.ValidationError(
                {
                    "enroll_as": (
                        "This account is registered as "
                        f"{user.enroll_as}. Switch the Individual / Organization tab."
                    )
                }
            )
        if not user.password_hash:
            raise serializers.ValidationError(
                {
                    "password": (
                        "This account was created with Microsoft Teams. "
                        "Use Teams login instead."
                    )
                }
            )
        if not user.check_password(password):
            raise serializers.ValidationError({"password": "Incorrect password."})
        attrs["email"] = email
        attrs["user"] = user
        return attrs
