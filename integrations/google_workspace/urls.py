from django.urls import path

from . import addon, views

urlpatterns = [
    path("", views.home, name="google-workspace-home"),
    path("login/", views.login, name="google-workspace-login"),
    path("join/", views.join, name="google-workspace-join"),
    path("callback/", views.callback, name="google-workspace-callback"),
    path("action/", views.action, name="google-workspace-action"),
    path("setup/", views.setup, name="google-workspace-setup"),
    path("signout/", views.signout, name="google-workspace-signout"),
    path("go/", views.go, name="google-workspace-go"),
    path("invite/", views.invite, name="google-workspace-invite"),
    path("remind/", views.remind, name="google-workspace-remind"),
    path("export/", views.export, name="google-workspace-export"),
    path("plans/", views.plans, name="google-workspace-plans"),
    path("addon/", addon.home, name="google-workspace-addon"),
    path("addon/action/", addon.action, name="google-workspace-addon-action"),
    path("addon/message/", addon.message, name="google-workspace-addon-message"),
]
