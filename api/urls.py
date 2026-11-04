from functools import wraps

from django.conf import settings
from django.http import HttpResponseNotFound
from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from . import teams_portal, aup_views, avatar, auth_views, billing_views, licence_views, location_views, org_policy_views, slack_card_views, slack_interactions, slack_onboarding, teams_admin_extra_views, teams_app_views, teams_bot_views, views, waitlist_views, aup_upload_views


def _legacy_teams(view):
    """The older Teams endpoints trust an email in the request (and the bot endpoint
    checks no signature), so they're off unless MS_TEAMS_LEGACY_ENABLED is set."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not settings.MS_TEAMS_LEGACY_ENABLED:
            return HttpResponseNotFound()
        return view(request, *args, **kwargs)
    return wrapper


urlpatterns = [
    path("", views.api_index, name="api-index"),
    path("health/", views.health_check, name="health-check"),
    # Swagger / OpenAPI docs
    path("schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="api-schema"), name="api-docs"),
    path("redoc/", SpectacularRedocView.as_view(url_name="api-schema"), name="api-redoc"),
    path("items/", views.ItemListCreateView.as_view(), name="item-list-create"),
    path("items/<str:pk>/", views.ItemDetailView.as_view(), name="item-detail"),
    # Teams / Microsoft auth
    path("auth/teams/login/", auth_views.teams_login, name="teams-login"),
    path(
        "auth/teams/login-redirect/",
        auth_views.teams_login_redirect,
        name="teams-login-redirect",
    ),
    path("auth/teams/callback/", auth_views.teams_callback, name="teams-callback"),
    # Azure-configured production callback path
    path(
        "admin/users/microsoft-teams/callback/",
        auth_views.teams_callback,
        name="teams-callback-azure",
    ),
    path("auth/teams/launch/", auth_views.teams_launch, name="teams-launch"),
    # Sign in with Slack (OpenID Connect)
    path("auth/slack/login/", auth_views.slack_login, name="slack-login"),
    path("auth/slack/callback/", auth_views.slack_callback, name="slack-callback"),
    path("auth/signup/", auth_views.signup, name="auth-signup"),
    path("auth/signin/", auth_views.signin, name="auth-signin"),
    path("auth/login/", auth_views.login, name="auth-login"),
    path("auth/signup/verify/", auth_views.signup_verify, name="auth-signup-verify"),
    path("auth/signin/verify/", auth_views.signin_verify, name="auth-signin-verify"),
    path("auth/captcha/", auth_views.captcha, name="auth-captcha"),
    path("auth/otp/resend/", auth_views.otp_resend, name="auth-otp-resend"),
    path("auth/me/", auth_views.me, name="auth-me"),
    path("auth/refresh/", auth_views.refresh, name="auth-refresh"),
    path("auth/logout/", auth_views.logout, name="auth-logout"),
    # Signup form dropdowns — Country -> State -> City
    path("licenses/<str:number>.png", licence_views.licence_png, name="licence-png"),
    path("cards/<str:kind>/<str:org_id>.png", licence_views.card_png, name="card-png"),
    path("avatar/start/", avatar.avatar_start, name="avatar-start"),
    path("avatar/", avatar.avatar_api, name="avatar"),
    path("billing/plans/", billing_views.plans_view, name="billing-plans"),
    path("billing/preview/", billing_views.preview_view, name="billing-preview"),
    path("billing/checkout/", billing_views.checkout_view, name="billing-checkout"),
    path("billing/stripe/webhook/", billing_views.stripe_webhook, name="billing-stripe-webhook"),
    path("waitlist/", waitlist_views.join, name="waitlist"),
    path("aup/upload/", aup_upload_views.upload, name="aup-upload"),
    path("aup/upload/publish/", aup_upload_views.publish, name="aup-upload-publish"),
    path("aup/questions/", aup_views.questions, name="aup-questions"),
    path("aup/preview/", aup_views.preview, name="aup-preview"),
    path("aup/banner.png", licence_views.aup_banner_png, name="aup-banner-png"),
    path("locations/countries/", location_views.countries, name="location-countries"),
    path("locations/states/", location_views.states, name="location-states"),
    path("locations/cities/", location_views.cities, name="location-cities"),
    # Organization policy answers (Slack guide 4.7)
    path("org/policy-answers/", org_policy_views.policy_answers, name="org-policy-answers"),
    # Slack Admin cards + User cards (AIDL Slack guide, sections 8 and 10)
    path("slack/cards/admin/", slack_card_views.slack_admin_cards, name="slack-admin-cards"),
    path("slack/cards/admin/coverage/", slack_card_views.slack_admin_coverage, name="slack-admin-coverage"),
    path(
        "slack/cards/admin/coverage.csv",
        slack_card_views.slack_admin_coverage_csv,
        name="slack-admin-coverage-csv",
    ),
    path("slack/cards/user/", slack_card_views.slack_user_cards, name="slack-user-cards"),
    path("slack/interactions/", slack_interactions.slack_interactions, name="slack-interactions"),
    path("slack/events/", slack_onboarding.slack_events, name="slack-events"),
    # AIDL in Teams without a bot: the pages channel cards open
    path("teams/app/", teams_portal.home, name="teams-app"),
    path("teams/app/login/", teams_portal.login, name="teams-app-login"),
    path("teams/app/logout/", teams_portal.logout, name="teams-app-logout"),
    path("teams/app/signed-out/", teams_portal.signed_out, name="teams-app-signed-out"),
    path("teams/app/dispatch/", teams_portal.dispatch_view, name="teams-app-dispatch"),
    path("teams/app/me/", teams_portal.me, name="teams-app-me"),
    path("teams/app/me/action/", teams_portal.me_action, name="teams-app-me-action"),
    path("teams/app/me/cards/", teams_portal.my_cards_moved),
    path("teams/app/me/cards/<str:no>/", teams_portal.card_page, name="teams-app-card"),
    path("teams/app/me/request-app/", teams_portal.request_app, name="teams-app-request"),
    path("teams/app/admin/", teams_portal.admin_home, name="teams-app-admin"),
    path("teams/app/admin/action/", teams_portal.admin_action, name="teams-app-admin-action"),
    path("teams/app/admin/timezone/", teams_portal.admin_timezone, name="teams-app-timezone"),
    path("teams/app/admin/plans/", teams_portal.plans_link, name="teams-app-plans"),
    path("teams/app/admin/coverage.csv", teams_portal.coverage, name="teams-app-coverage"),
    path("teams/app/admin/policy/", teams_portal.policy, name="teams-app-policy"),
    path("teams/app/admin/policy/upload/", teams_portal.upload_page, name="teams-app-upload"),
    path("teams/app/admin/policy/upload/publish/", teams_portal.upload_publish, name="teams-app-upload-publish"),
    path("teams/app/admin/policy/<int:n>/", teams_portal.policy_card, name="teams-app-policy-card"),
    path("teams/app/admin/regulators/", teams_portal.regulators_page, name="teams-app-regulators"),
    path("teams/app/admin/logo/", teams_portal.logo, name="teams-app-logo"),
    path("teams/app/admin/admins/", teams_portal.admins, name="teams-app-admins"),
    path("teams/app/admin/team/", teams_portal.team, name="teams-app-team"),
    path("teams/app/admin/cards/", teams_portal.cards, name="teams-app-cards"),
    path("teams/app/admin/cards/action/", teams_portal.cards_action, name="teams-app-cards-action"),
    path("teams/app/admin/apps/", teams_portal.apps, name="teams-app-apps"),
    path("teams/app/admin/apps/decision/", teams_portal.app_decision, name="teams-app-app-decision"),
    path("teams/app/admin/alerts/", teams_portal.alerts, name="teams-app-alerts"),
    # AIDL Teams app — menu tabs + Adaptive Cards
    path("teams/", teams_app_views.teams_app_index, name="teams-app-index"),
    path(
        "teams/bot/messages/",
        _legacy_teams(teams_bot_views.teams_bot_messages),
        name="teams-bot-messages",
    ),
    path("teams/tabs/<str:tab>/", _legacy_teams(teams_app_views.teams_tab_page), name="teams-tab-page"),
    path(
        "teams/admin/export/",
        _legacy_teams(teams_app_views.teams_admin_export_csv),
        name="teams-admin-export",
    ),
    path(
        "teams/admin/invite/",
        teams_app_views.teams_admin_invite,
        name="teams-admin-invite",
    ),
    path(
        "teams/admin/session/",
        teams_app_views.teams_admin_session,
        name="teams-admin-session",
    ),
    path(
        "teams/admin/invite-user/",
        teams_app_views.teams_invite_user,
        name="teams-invite-user",
    ),
    path(
        "teams/admin/team-members/",
        teams_app_views.teams_admin_team_members,
        name="teams-admin-team-members",
    ),
    path(
        "teams/admin/aup/sign/",
        teams_app_views.teams_admin_sign_aup,
        name="teams-admin-sign-aup",
    ),
    path(
        "teams/admin/licence/issue/",
        teams_app_views.teams_admin_issue_licence,
        name="teams-admin-issue-licence",
    ),
    # Policy upload/versioning
    path(
        "teams/admin/policy/upload/",
        teams_admin_extra_views.teams_admin_policy_upload,
        name="teams-admin-policy-upload",
    ),
    path(
        "teams/admin/policy/file/<str:version_id>/",
        teams_admin_extra_views.teams_admin_policy_file,
        name="teams-admin-policy-file",
    ),
    # Cards catalogue — request / send (now or later) / request a new card
    path(
        "teams/admin/cards/request-new/",
        teams_admin_extra_views.teams_admin_cards_request_new,
        name="teams-admin-cards-request-new",
    ),
    path(
        "teams/admin/cards/<str:card_id>/request/",
        teams_admin_extra_views.teams_admin_cards_request,
        name="teams-admin-cards-request",
    ),
    path(
        "teams/admin/cards/<str:card_id>/send/",
        teams_admin_extra_views.teams_admin_cards_send,
        name="teams-admin-cards-send",
    ),
    # AI Apps / IT Apps — Add Application
    path(
        "teams/admin/ai-apps/add/",
        teams_admin_extra_views.teams_admin_app_add,
        {"app_type": "ai"},
        name="teams-admin-ai-apps-add",
    ),
    path(
        "teams/admin/it-apps/add/",
        teams_admin_extra_views.teams_admin_app_add,
        {"app_type": "it"},
        name="teams-admin-it-apps-add",
    ),
    path(
        "teams/admin/<str:tab>/",
        _legacy_teams(teams_app_views.teams_admin_json),
        name="teams-admin-json",
    ),
    path("teams/cards/<str:tab>/", _legacy_teams(teams_app_views.teams_card_json), name="teams-card-json"),
    path(
        "teams/tab-content/<str:tab>/",
        _legacy_teams(teams_app_views.teams_tab_content_json),
        name="teams-tab-content-json",
    ),
    path(
        "teams/channel-tabs/install/",
        teams_app_views.teams_install_channel_tabs,
        name="teams-install-channel-tabs",
    ),
    path(
        "teams/welcome/send/",
        teams_app_views.teams_send_welcome,
        name="teams-send-welcome",
    ),
    path(
        "teams/cards/<str:tab>/send/",
        teams_app_views.teams_send_card,
        name="teams-send-card",
    ),
    path(
        "teams/cards/learners-permit/download/",
        _legacy_teams(teams_app_views.teams_licence_download),
        name="teams-licence-download",
    ),
    path(
        "teams/cards/traffic-light-check/rate/",
        _legacy_teams(teams_app_views.teams_traffic_light_rate),
        name="teams-traffic-light-rate",
    ),
]
