from django.urls import path

from . import views

urlpatterns = [
    path("jwks/", views.jwks, name="lti-jwks"),
]
