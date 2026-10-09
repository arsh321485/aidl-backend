import re
import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.conf import settings
from rest_framework import serializers

from .models import AIDLUser, Item

_NAME_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ \-'.])*$", re.UNICODE)
_MOBILE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{6,19}$")
# Any symbol counts (same rule as the website's live hint): anything that is
# not a letter, digit or space.
_PASSWORD_SPECIAL_RE = re.compile(r"[^A-Za-z0-9\s]")


def password_complexity_errors(password: str) -> list[str]:
    errors = []
    if not re.search(r"[A-Z]", password):
        errors.append("Password must include at least one uppercase letter (A-Z).")
    if not re.search(r"[a-z]", password):
        errors.append("Password must include at least one lowercase letter (a-z).")
    if not re.search(r"[0-9]", password):
        errors.append("Password must include at least one number (0-9).")
    if not _PASSWORD_SPECIAL_RE.search(password):
        errors.append(
            "Password must include at least one special character "
            "(!@#$%^&*_- etc.)."
        )
    return errors
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
    # Guide 4.7 — has this user's organization answered the 8 policy questions?
    policy_completed = serializers.SerializerMethodField()

    def get_policy_completed(self, user) -> bool:
        from .org_policy import policy_completed
        from .org_service import get_organization_for_user

        if not user.organization_id:
            return False
        return policy_completed(get_organization_for_user(user))

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
            "licence_number",
            "licence_issued_at",
            "licence_expires_at",
            "aup_signed",
            "aup_signed_at",
            "teams_team_id",
            "teams_channel_id",
            "teams_channel_name",
            "is_active",
            "last_login_at",
            "created_at",
            "updated_at",
            "policy_completed",
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
    # Optional: the website signs people in with an emailed code instead of a
    # password. When one is sent, the strength rules below still apply.
    password = serializers.CharField(write_only=True, max_length=128, required=False, allow_blank=True, default="")
    confirm_password = serializers.CharField(write_only=True, max_length=128, required=False, allow_blank=True, default="")
    # Optional: the website no longer asks for it.
    mobile_number = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
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
        # Sign-ups that never entered their email code don't block the address.
        if AIDLUser.objects.filter(email__iexact=email).exclude(pk__in=_pending_signups(email)).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email

    def validate_mobile_number(self, value: str) -> str:
        mobile = re.sub(r"\s+", " ", (value or "").strip())
        if mobile and not _MOBILE_RE.fullmatch(mobile):
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
        if not password and not settings.AUTH_EMAIL_OTP:
            raise serializers.ValidationError({"password": "Choose a password."})
        if password and password != (attrs.get("confirm_password") or ""):
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )

        dummy = AIDLUser(
            email=attrs.get("email", ""),
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
            full_name=f"{attrs.get('first_name', '')} {attrs.get('last_name', '')}".strip(),
        )
        password_errors = password_complexity_errors(password) if password else []
        if password:
            if len(password) < 8:
                password_errors.insert(0, "Password must be at least 8 characters.")
            try:
                validate_password(password, user=dummy)
            except DjangoValidationError as exc:
                password_errors.extend(exc.messages)
        if password_errors:
            raise serializers.ValidationError({"password": password_errors})

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
        AIDLUser.objects.filter(pk__in=_pending_signups(validated_data["email"])).delete()
        user = AIDLUser(
            microsoft_id=f"local:{uuid.uuid4()}",
            provider="website",
            full_name=f"{first_name} {last_name}".strip(),
            role=role,
            # Inactive until the emailed code is entered (auth_verification.py).
            is_active=not settings.AUTH_EMAIL_OTP,
            **validated_data,
        )
        if password:
            user.set_password(password)
        user.save()
        return user


def _pending_signups(email: str) -> list:
    """Website accounts created but never verified with the emailed code."""
    return list(AIDLUser.objects.filter(email__iexact=email, provider="website", is_active=False,
                                        last_login_at__isnull=True).values_list("pk", flat=True))


class LoginSerializer(serializers.Serializer):
    """Website sign-in — email, password, and Individual / Organization tab."""

    enroll_as = serializers.ChoiceField(choices=AIDLUser.EnrollAs.choices)
    email = serializers.EmailField()
    # Optional: without it the sign-in is finished with the emailed code.
    password = serializers.CharField(write_only=True, max_length=128, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        email = (attrs.get("email") or "").strip().lower()
        password = attrs.get("password") or ""
        enroll_as = attrs.get("enroll_as") or AIDLUser.EnrollAs.INDIVIDUAL
        user = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
        if user is None and _pending_signups(email):
            raise serializers.ValidationError(
                {"email": "This email was never confirmed — register again to get a new code."}
            )
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
        if not password:
            if not settings.AUTH_EMAIL_OTP:
                raise serializers.ValidationError({"password": "Enter your password."})
            attrs["email"] = email
            attrs["user"] = user
            return attrs  # passwordless: the emailed code proves it's them
        if not user.password_hash:
            raise serializers.ValidationError(
                {"password": "This account doesn't use a password — leave it empty and use the emailed code."}
            )
        if not user.check_password(password):
            raise serializers.ValidationError({"password": "Incorrect password."})
        attrs["email"] = email
        attrs["user"] = user
        return attrs
