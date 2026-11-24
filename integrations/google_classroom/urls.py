from django.urls import path

from . import admin_views, policy_views, school_views, setup_views, views

urlpatterns = [
    # step 1: sign up / sign in
    path("login/", views.login, name="google-classroom-login"),
    path("callback/", views.callback, name="google-classroom-callback"),
    path("open/", views.open_classroom, name="google-classroom-open"),
    # the school's short setup after sign-up: details (approx. students) → AI policy → done
    path("admin/setup/", setup_views.details, name="google-classroom-setup"),
    path("admin/setup/policy/", setup_views.policy, name="google-classroom-setup-policy"),
    path("admin/setup/done/", setup_views.done, name="google-classroom-setup-done"),
    # step 2: classes + students
    path("classes/", views.classes_page, name="google-classroom-classes"),
    path("classes/connect/", views.classes_connect, name="google-classroom-classes-connect"),
    path("classes/sync/", views.classes_sync, name="google-classroom-classes-sync"),
    path("classes/choose/", views.classes_choose, name="google-classroom-classes-choose"),
    path("classes/invite/", views.classes_invite, name="google-classroom-classes-invite"),
    path("classes/invite-emails/", views.classes_invite_emails, name="google-classroom-classes-invite-emails"),
    path("classes/setup/", views.classes_setup, name="google-classroom-classes-setup"),
    path("classes/signout/", views.classes_signout, name="google-classroom-classes-signout"),
    path("api/classes/", views.api_classes, name="google-classroom-api-classes"),
    path("api/classes/sync/", views.api_classes_sync, name="google-classroom-api-classes-sync"),
    path("api/classes/choose/", views.api_classes_choose, name="google-classroom-api-classes-choose"),
    # step 4: learners
    path("learn/", views.learn, name="google-classroom-learn"),
    path("learn/action/", views.learn_action, name="google-classroom-learn-action"),
    # step 5: Admin Center
    path("admin/", admin_views.home, name="google-classroom-admin"),
    path("admin/step/<int:n>/", admin_views.step, name="google-classroom-admin-step"),
    path("admin/action/", admin_views.action, name="google-classroom-admin-action"),
    path("admin/team/", admin_views.team, name="google-classroom-admin-team"),
    path("admin/team/action/", admin_views.team_action, name="google-classroom-admin-team-action"),
    path("admin/admins/", admin_views.admins, name="google-classroom-admin-admins"),
    path("admin/admins/action/", admin_views.admins_action, name="google-classroom-admin-admins-action"),
    path("admin/upgrade/", admin_views.upgrade, name="google-classroom-admin-upgrade"),
    path("admin/plans/", admin_views.plans, name="google-classroom-admin-plans"),
    path("admin/coverage.csv", admin_views.coverage, name="google-classroom-admin-coverage"),
    # Enterprise: the whole school (the IT admin adds every student)
    path("admin/school/", school_views.page, name="google-classroom-admin-school"),
    path("admin/school/action/", school_views.action, name="google-classroom-admin-school-action"),
    path("admin/school/connect/", school_views.connect, name="google-classroom-admin-school-connect"),
    path("school/it-admin/", school_views.it_admin, name="google-classroom-school-it-admin"),
    path("school/it-admin/go/", school_views.it_admin_go, name="google-classroom-school-it-admin-go"),
    # step 6: Admin Center, AI policy
    path("admin/policy/", policy_views.policy, name="google-classroom-admin-policy"),
    path("admin/policy/<int:n>/", policy_views.policy_card, name="google-classroom-admin-policy-card"),
    path("admin/regulators/", policy_views.regulators_page, name="google-classroom-admin-regulators"),
    path("admin/upload/", policy_views.upload, name="google-classroom-admin-upload"),
    path("admin/upload/publish/", policy_views.upload_publish, name="google-classroom-admin-upload-publish"),
    path("admin/logo/", policy_views.logo, name="google-classroom-admin-logo"),
    path("admin/apps/", policy_views.apps, name="google-classroom-admin-apps"),
]
