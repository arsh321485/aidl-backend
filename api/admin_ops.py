"""Admin Center write operations shared between the REST endpoints (used by
the Website Tab) and the Teams bot's Adaptive Card actions (used by the
in-Teams native flow) — one place for the business logic + validation so
both entry points behave identically."""

from __future__ import annotations

from .models import AIDLUser, Organization, RegisteredApp


class AdminOpsError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(message or code)


def promote_to_admin(caller: AIDLUser, *, email: str, permissions: dict | None = None) -> AIDLUser:
    email = (email or "").strip().lower()
    if not email:
        raise AdminOpsError("email_required")
    if not caller.organization_id:
        raise AdminOpsError("no_organization")
    if caller.role != AIDLUser.Role.ADMIN:
        raise AdminOpsError("not_admin", "Only an existing admin can add another admin.")

    target = AIDLUser.objects.filter(email__iexact=email, is_active=True).first()
    if target is None:
        raise AdminOpsError(
            "user_not_found", "User must sign in via Teams once before becoming admin."
        )

    admins = AIDLUser.objects.filter(
        organization_id=caller.organization_id,
        role=AIDLUser.Role.ADMIN,
        is_active=True,
    ).count()
    org = Organization.objects.filter(pk=caller.organization_id).first()
    limit = org.admin_seat_limit if org else 3
    if target.role != AIDLUser.Role.ADMIN and admins >= limit:
        raise AdminOpsError("admin_seats_full", f"Admin seat limit ({limit}) reached.")

    target.organization_id = caller.organization_id
    target.organization_name = caller.organization_name or (org.name if org else "")
    target.role = AIDLUser.Role.ADMIN
    update_fields = ["organization_id", "organization_name", "role", "updated_at"]
    if isinstance(permissions, dict):
        for key, field in (
            ("approve_apps", "perm_approve_apps"),
            ("access_cards", "perm_access_cards"),
            ("create_card", "perm_create_card"),
        ):
            if key in permissions:
                setattr(target, field, bool(permissions[key]))
                update_fields.append(field)
    target.save(update_fields=update_fields)
    return target


VALID_DATA_ALLOWED = {c for c, _ in RegisteredApp.DataAllowed.choices}
VALID_APP_STATUS = {c for c, _ in RegisteredApp.Status.choices}


def add_registered_app(
    org: Organization,
    *,
    app_type: str,
    name: str,
    category: str = "",
    data_allowed: str = "",
    status: str = "",
    description: str = "",
) -> RegisteredApp:
    if app_type not in (RegisteredApp.AppType.AI, RegisteredApp.AppType.IT):
        raise AdminOpsError("unknown_app_type")
    name = (name or "").strip()
    if not name:
        raise AdminOpsError("name_required")

    data_allowed = (data_allowed or "").strip().lower().replace(" ", "_")
    if data_allowed not in VALID_DATA_ALLOWED:
        data_allowed = ""
    status = (status or RegisteredApp.Status.PENDING).strip().lower()
    if status not in VALID_APP_STATUS:
        status = RegisteredApp.Status.PENDING

    return RegisteredApp.objects.create(
        organization_id=str(org.pk),
        name=name,
        app_type=app_type,
        status=status,
        category=category.strip(),
        data_allowed=data_allowed,
        description=description.strip(),
    )
