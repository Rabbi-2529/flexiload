import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SimProfile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                (
                    "operator",
                    models.CharField(
                        choices=[
                            ("grameenphone", "Grameenphone"),
                            ("robi", "Robi"),
                            ("airtel", "Airtel"),
                            ("banglalink", "Banglalink"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "port",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "Optional serial port override, for example COM3 "
                            "or /dev/ttyUSB0."
                        ),
                        max_length=100,
                    ),
                ),
                ("encrypted_pin", models.TextField()),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sim_profiles",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="TopupTransaction",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "operator",
                    models.CharField(
                        choices=[
                            ("grameenphone", "Grameenphone"),
                            ("robi", "Robi"),
                            ("airtel", "Airtel"),
                            ("banglalink", "Banglalink"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "product",
                    models.CharField(
                        choices=[
                            ("flexiload", "Flexiload"),
                            ("offer", "Offer"),
                            ("postpaid", "Postpaid"),
                            ("prepaid", "Prepaid"),
                            ("regular", "Regular"),
                            ("skitto", "Skitto"),
                            ("topup", "Top-up"),
                        ],
                        max_length=20,
                    ),
                ),
                ("phone_number", models.CharField(max_length=14)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("success", "Success"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("modem_response", models.TextField(blank=True)),
                ("error_message", models.TextField(blank=True)),
                (
                    "idempotency_key",
                    models.CharField(blank=True, max_length=100, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "sim_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transactions",
                        to="topups.simprofile",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="topup_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="simprofile",
            constraint=models.UniqueConstraint(
                fields=("owner", "name"),
                name="unique_sim_name_per_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="topuptransaction",
            constraint=models.UniqueConstraint(
                fields=("user", "idempotency_key"),
                name="unique_user_idempotency_key",
            ),
        ),
    ]
