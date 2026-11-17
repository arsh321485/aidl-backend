from django.urls import path

from . import views

urlpatterns = [
    # step 1: sign up / sign in
    path("login/", views.login, name="google-classroom-login"),
    path("callback/", views.callback, name="google-classroom-callback"),
    # step 2: classes + students
    path("classes/", views.classes_page, name="google-classroom-classes"),
    path("classes/connect/", views.classes_connect, name="google-classroom-classes-connect"),
    path("classes/sync/", views.classes_sync, name="google-classroom-classes-sync"),
    path("classes/signout/", views.classes_signout, name="google-classroom-classes-signout"),
    path("api/classes/", views.api_classes, name="google-classroom-api-classes"),
    path("api/classes/sync/", views.api_classes_sync, name="google-classroom-api-classes-sync"),
]
