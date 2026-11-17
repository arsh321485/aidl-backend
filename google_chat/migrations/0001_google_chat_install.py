import django_mongodb_backend.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="GoogleChatInstall",
            fields=[
                ("id", django_mongodb_backend.fields.ObjectIdAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("organization_id", models.CharField(max_length=64, unique=True)),
                ("hosted_domain", models.CharField(max_length=255, unique=True)),
                ("access_token", models.TextField(blank=True, default="")),
                ("refresh_token", models.TextField(blank=True, default="")),
                ("token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("space_name", models.CharField(blank=True, default="", max_length=128)),
                ("space_url", models.CharField(blank=True, default="", max_length=500)),
                ("home_message_name", models.CharField(blank=True, default="", max_length=256)),
                ("admin_google_sub", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
