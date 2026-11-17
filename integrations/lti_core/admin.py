from django.contrib import admin

from .models import LtiPlatform


@admin.register(LtiPlatform)
class LtiPlatformAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "issuer", "client_id", "organization_id", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name", "issuer", "client_id")
    readonly_fields = ("organization_id", "created_at", "updated_at")
