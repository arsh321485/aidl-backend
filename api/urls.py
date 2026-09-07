from django.urls import path

from . import auth_views, teams_app_views, teams_bot_views, views

urlpatterns = [
    path("", views.api_index, name="api-index"),
    path("health/", views.health_check, name="health-check"),
    path("items/", views.ItemListCreateView.as_view(), name="item-list-create"),
    path("items/<str:pk>/", views.ItemDetailView.as_view(), name="item-detail"),
    # Teams / Microsoft auth
    path("auth/teams/login/", auth_views.teams_login, name="teams-login"),
    path("auth/teams/callback/", auth_views.teams_callback, name="teams-callback"),
    # Azure-configured production callback path
    path(
        "admin/users/microsoft-teams/callback/",
        auth_views.teams_callback,
        name="teams-callback-azure",
    ),
    path("auth/teams/launch/", auth_views.teams_launch, name="teams-launch"),
    path("auth/me/", auth_views.me, name="auth-me"),
    path("auth/refresh/", auth_views.refresh, name="auth-refresh"),
    path("auth/logout/", auth_views.logout, name="auth-logout"),
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
        "teams/admin/aup/sign/",
        teams_app_views.teams_admin_sign_aup,
        name="teams-admin-sign-aup",
    ),
    path(
        "teams/admin/licence/issue/",
        teams_app_views.teams_admin_issue_licence,
        name="teams-admin-issue-licence",
    ),
    path(
        "teams/admin/<str:tab>/",
        teams_app_views.teams_admin_json,
        name="teams-admin-json",
    ),
    path("teams/cards/<str:tab>/", teams_app_views.teams_card_json, name="teams-card-json"),
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
]
