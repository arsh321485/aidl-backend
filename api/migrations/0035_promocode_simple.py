"""The internal testing code becomes one readable code (e.g. TESTING_QA) with ON/OFF and
a plan; which workspaces may use it is settings.INTERNAL_SLACK_TEAMS. Old bound codes
are removed (they only worked with the code shown once, which is gone)."""

from django.db import migrations, models


def drop_old_codes(apps, schema_editor):
    apps.get_model("api", "PromoCode").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0034_promocode_slack_binding'),
    ]

    operations = [
        migrations.RunPython(drop_old_codes, migrations.RunPython.noop),
        migrations.RemoveField(model_name='promocode', name='code_hash'),
        migrations.RemoveField(model_name='promocode', name='label'),
        migrations.RemoveField(model_name='promocode', name='allowed_slack_teams'),
        migrations.RemoveField(model_name='promocode', name='allowed_emails'),
        migrations.RemoveField(model_name='promocode', name='expires_at'),
        migrations.RemoveField(model_name='promocode', name='max_uses'),
        migrations.RemoveField(model_name='promocode', name='uses'),
        migrations.AddField(
            model_name='promocode',
            name='code',
            field=models.CharField(default='', help_text="Letters, digits, _ or -, e.g. TESTING_QA. Capitals don't matter.",
                                   max_length=40, unique=True),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='promocode',
            name='is_active',
            field=models.BooleanField(default=True, verbose_name='ON'),
        ),
        migrations.AlterField(
            model_name='promocode',
            name='plan',
            field=models.CharField(choices=[('enterprise', 'Enterprise'), ('basic', 'Basic')], default='enterprise',
                                   max_length=20),
        ),
        migrations.CreateModel(
            name='InternalTestOrganization',
            fields=[],
            options={'verbose_name': 'internal test organization', 'proxy': True, 'indexes': [], 'constraints': []},
            bases=('api.organization',),
        ),
    ]
