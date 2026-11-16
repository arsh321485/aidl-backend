"""The internal promo code is now one fixed setting (INTERNAL_PROMO_CODE, switched by INTERNAL_PROMO_ENABLED), so the
PromoCode table that Django Admin used to manage is not needed. It held nothing a customer could see or use."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0035_promocode_simple'),
    ]

    operations = [
        migrations.DeleteModel(name='PromoCode'),
    ]
