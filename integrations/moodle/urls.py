from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="moodle-home"),
    path("connect/", views.connect, name="moodle-connect"),
    path("login/", views.login, name="moodle-login"),
    path("launch/", views.launch, name="moodle-launch"),
    path("action/", views.action, name="moodle-action"),
    path("setup/", views.setup, name="moodle-setup"),
]
