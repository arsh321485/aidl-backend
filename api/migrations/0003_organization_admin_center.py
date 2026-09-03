# Generated manually for Admin Center dynamic data

import django_mongodb_backend.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0002_aidluser_oauthstate"),
    ]

    operations = [
        migrations.CreateModel(
            name="Organization",
            fields=[
                (
                    "id",
                    django_mongodb_backend.fields.ObjectIdAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("slug", models.CharField(blank=True, default="", max_length=128, unique=True)),
                ("teams_team_id", models.CharField(blank=True, default="", max_length=128)),
                ("seats_purchased", models.IntegerField(default=50)),
                ("seats_renews_on", models.DateField(blank=True, null=True)),
                ("admin_seat_limit", models.IntegerField(default=3)),
                ("rollout_steps_done", models.IntegerField(default=3)),
                ("rollout_steps_total", models.IntegerField(default=4)),
                (
                    "policy_title",
                    models.CharField(
                        blank=True,
                        default="Acceptable Use of Technology Policy",
                        max_length=255,
                    ),
                ),
                ("policy_url", models.URLField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="RegisteredApp",
            fields=[
                (
                    "id",
                    django_mongodb_backend.fields.ObjectIdAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("organization_id", models.CharField(db_index=True, max_length=64)),
                ("name", models.CharField(max_length=255)),
                (
                    "app_type",
                    models.CharField(
                        choices=[("ai", "AI"), ("it", "IT")],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("approved", "Approved"),
                            ("pending", "Pending"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="aidluser",
            name="role",
            field=models.CharField(
                choices=[("admin", "Admin"), ("learner", "Learner")],
                default="learner",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="aidluser",
            name="organization_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="aidluser",
            name="organization_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="aidluser",
            name="licence_issued",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="aidluser",
            name="aup_signed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="aidluser",
            name="aup_signed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
