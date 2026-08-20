from django.urls import path

from . import auth_views, views

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
]
