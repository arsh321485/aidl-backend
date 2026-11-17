from django.apps import AppConfig


class LtiCoreConfig(AppConfig):
    default_auto_field = "django_mongodb_backend.fields.ObjectIdAutoField"
    name = "integrations.lti_core"
    label = "lti_core"
    verbose_name = "LTI 1.3 (shared by Moodle, Canvas, Schoology, Brightspace, Blackboard)"
