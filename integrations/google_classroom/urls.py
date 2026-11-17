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
    path("classes/choose/", views.classes_choose, name="google-classroom-classes-choose"),
    path("classes/setup/", views.classes_setup, name="google-classroom-classes-setup"),
    path("classes/signout/", views.classes_signout, name="google-classroom-classes-signout"),
    path("api/classes/", views.api_classes, name="google-classroom-api-classes"),
    path("api/classes/sync/", views.api_classes_sync, name="google-classroom-api-classes-sync"),
    path("api/classes/choose/", views.api_classes_choose, name="google-classroom-api-classes-choose"),
    # step 4: learners
    path("learn/", views.learn, name="google-classroom-learn"),
    path("learn/action/", views.learn_action, name="google-classroom-learn-action"),
]
