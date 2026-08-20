from django.contrib import admin

from .models import AIDLUser, Item, OAuthState


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
