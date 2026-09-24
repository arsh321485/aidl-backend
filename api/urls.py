from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from . import auth_views, location_views, teams_admin_extra_views, teams_app_views, teams_bot_views, views

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
    path("auth/signup/", auth_views.signup, name="auth-signup"),
    path("auth/signin/", auth_views.signin, name="auth-signin"),
    path("auth/login/", auth_views.login, name="auth-login"),
    path("auth/me/", auth_views.me, name="auth-me"),
    path("auth/refresh/", auth_views.refresh, name="auth-refresh"),
    path("auth/logout/", auth_views.logout, name="auth-logout"),
    # Signup form dropdowns — Country -> State -> City
    path("locations/countries/", location_views.countries, name="location-countries"),
    path("locations/states/", location_views.states, name="location-states"),
    path("locations/cities/", location_views.cities, name="location-cities"),
    # AIDL Teams app — menu tabs + Adaptive Cards
    path("teams/", teams_app_views.teams_app_index, name="teams-app-index"),
    path(
        "teams/bot/messages/",
        teams_bot_views.teams_bot_messages,
        name="teams-bot-messages",
    ),
    path("teams/tabs/<str:tab>/", teams_app_views.teams_tab_page, name="teams-tab-page"),
    path(
        "teams/admin/export/",
        teams_app_views.teams_admin_export_csv,
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
        teams_app_views.teams_admin_json,
        name="teams-admin-json",
    ),
    path("teams/cards/<str:tab>/", teams_app_views.teams_card_json, name="teams-card-json"),
    path(
        "teams/tab-content/<str:tab>/",
        teams_app_views.teams_tab_content_json,
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
        teams_app_views.teams_licence_download,
        name="teams-licence-download",
    ),
    path(
        "teams/cards/traffic-light-check/rate/",
        teams_app_views.teams_traffic_light_rate,
        name="teams-traffic-light-rate",
    ),
]
