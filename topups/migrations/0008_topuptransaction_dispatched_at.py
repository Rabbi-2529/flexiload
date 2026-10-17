# Adds dispatched_at for the one-at-a-time recharge queue.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('topups', '0007_storedmodemmessage_parsed_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='topuptransaction',
            name='dispatched_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
