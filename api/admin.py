from django import forms
from django.conf import settings
from django.contrib import admin

from .models import AIDLUser, InternalTestOrganization, Item, OAuthState, Organization, PromoCode


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
        "country",
        "license_class",
        "is_active",
        "last_login_at",
        "created_at",
    )
    list_filter = ("enroll_as", "provider", "license_class", "is_active")
    search_fields = ("email", "full_name", "first_name", "last_name", "mobile_number", "microsoft_id")


@admin.register(OAuthState)
class OAuthStateAdmin(admin.ModelAdmin):
    list_display = ("state", "enroll_as", "expires_at", "created_at")

@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    """The internal testing code: only our own Slack workspaces can use it (billing.redeem)."""

    list_display = ("code", "is_active", "plan")
    list_editable = ("is_active", "plan")
    fields = ("code", "is_active", "plan")


@admin.register(InternalTestOrganization)
class InternalTestOrganizationAdmin(admin.ModelAdmin):
    """Switch our own Slack test organizations between Trial and Enterprise (no payment)."""

    PLANS = [("trial", "Trial"), ("enterprise", "Enterprise")]
    list_display = ("name", "slack_team_id", "plan")
    list_editable = ("plan",)
    fields = ("name", "slack_team_id", "plan")
    readonly_fields = ("name", "slack_team_id")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(slack_team_id__in=sorted(settings.INTERNAL_SLACK_TEAMS))

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "plan":
            return forms.ChoiceField(choices=self.PLANS, label="Plan")
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if "plan" in form.changed_data:  # the same activation as a promo code, never a plain save
            from .billing import set_internal_plan

            set_internal_plan(Organization.objects.get(pk=obj.pk), obj.plan, request.user.get_username())

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
