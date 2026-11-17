"""Routes under /api/google-chat/."""

from django.urls import path

from . import views

urlpatterns = [
    path("auth/login/", views.google_login, name="google-chat-login"),
    path("auth/callback/", views.google_callback, name="google-chat-callback"),
]
