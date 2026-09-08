from django.contrib import admin

from .models import AIDLUser, CardDelivery, CardRequest, Item, OAuthState, Organization, RegisteredApp


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "description")


@admin.register(AIDLUser)
class AIDLUserAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "full_name",
        "enroll_as",
        "provider",
        "is_active",
        "last_login_at",
        "created_at",
    )
    list_filter = ("enroll_as", "provider", "is_active")
    search_fields = ("email", "full_name", "microsoft_id")


@admin.register(OAuthState)
class OAuthStateAdmin(admin.ModelAdmin):
    list_display = ("state", "enroll_as", "expires_at", "created_at")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "seats_purchased", "admin_card_sent", "is_active", "created_at")
    search_fields = ("name", "slug", "teams_team_id")


@admin.register(RegisteredApp)
class RegisteredAppAdmin(admin.ModelAdmin):
    list_display = ("name", "organization_id", "app_type", "status", "is_active")
    list_filter = ("app_type", "status")
    search_fields = ("name", "organization_id")


@admin.register(CardDelivery)
class CardDeliveryAdmin(admin.ModelAdmin):
    list_display = ("organization_id", "card_key", "status", "sent_at", "scheduled_at")
    list_filter = ("status",)
    search_fields = ("organization_id", "card_key")


@admin.register(CardRequest)
class CardRequestAdmin(admin.ModelAdmin):
    list_display = ("name", "organization_id", "priority", "status", "created_at")
    list_filter = ("priority", "status")
    search_fields = ("name", "organization_id", "requested_by_email")
