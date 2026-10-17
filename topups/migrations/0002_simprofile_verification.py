from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("topups", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="simprofile",
            name="sim_number",
            field=models.CharField(blank=True, max_length=14),
        ),
        migrations.AddField(
            model_name="simprofile",
            name="sim_verification_response",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="simprofile",
            name="sim_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
