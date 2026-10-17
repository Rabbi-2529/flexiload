# Generated for correct modem SMS reconciliation.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('topups', '0006_topuptransaction_last_retried_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='storedmodemmessage',
            name='operator',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='storedmodemmessage',
            name='carrier_transaction_id',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name='storedmodemmessage',
            name='parsed_status',
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
