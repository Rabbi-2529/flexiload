import logging
import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models, transaction as db_transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .crypto import decrypt_secret, encrypt_secret


logger = logging.getLogger("topups.transactions")


pin_validator = RegexValidator(
    r"^\d{4,12}$",
    "PIN must contain 4 to 12 digits.",
)


class Operator(models.TextChoices):
    GRAMEENPHONE = "grameenphone", "Grameenphone"
    ROBI = "robi", "Robi"
    AIRTEL = "airtel", "Airtel"
    BANGLALINK = "banglalink", "Banglalink"
    TELETALK = "teletalk", "Teletalk"


class Product(models.TextChoices):
    FLEXILOAD = "flexiload", "Flexiload"
    OFFER = "offer", "Offer"
    POSTPAID = "postpaid", "Postpaid"
    PREPAID = "prepaid", "Prepaid"
    REGULAR = "regular", "Regular"
    SKITTO = "skitto", "Skitto"
    TOPUP = "topup", "Top-up"


class SimProfile(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sim_profiles",
    )
    name = models.CharField(max_length=100)
    operator = models.CharField(
        max_length=20,
        choices=Operator.choices,
    )
    port = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Optional serial port override, "
            "for example COM3 or /dev/ttyUSB0."
        ),
    )
    encrypted_pin = models.TextField()
    sim_number = models.CharField(max_length=14, blank=True)
    sim_verified_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    sim_verification_response = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"],
                name="unique_sim_name_per_owner",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.get_operator_display()})"

    def set_pin(self, pin):
        pin_validator(pin)
        self.encrypted_pin = encrypt_secret(pin)

    def get_pin(self):
        return decrypt_secret(self.encrypted_pin)


class TransactionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"


class TopupTransaction(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="topup_transactions",
    )
    sim_profile = models.ForeignKey(
        SimProfile,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    operator = models.CharField(
        max_length=20,
        choices=Operator.choices,
    )
    product = models.CharField(
        max_length=20,
        choices=Product.choices,
    )
    phone_number = models.CharField(max_length=14)
    amount = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20,
        choices=TransactionStatus.choices,
        default=TransactionStatus.PENDING,
        db_index=True,
    )
    modem_response = models.TextField(blank=True)
    error_message = models.TextField(blank=True)
    idempotency_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    # Retry audit fields
    retry_count = models.PositiveIntegerField(default=0)
    last_retried_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )
    dispatched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the queued top-up was actually sent to the modem.",
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                name="unique_user_idempotency_key",
            )
        ]

    def __str__(self):
        return f"{self.id} - {self.phone_number} - {self.status}"


class StoredModemMessage(models.Model):
    sim_profile = models.ForeignKey(
        SimProfile,
        on_delete=models.CASCADE,
        related_name="stored_messages",
    )
    fingerprint = models.CharField(
        max_length=64,
        unique=True,
    )
    modem_index = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=30,
        blank=True,
    )
    sender = models.CharField(
        max_length=100,
        blank=True,
    )
    operator = models.CharField(
        max_length=20,
        blank=True,
    )
    modem_timestamp = models.CharField(
        max_length=50,
        blank=True,
    )
    text = models.TextField()
    carrier_transaction_id = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
    )
    parsed_status = models.CharField(
        max_length=20,
        blank=True,
    )
    stored_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        ordering = ["-stored_at", "-id"]

    def __str__(self):
        sender = self.sender or "Unknown sender"
        return f"{self.sim_profile} - {sender}"


# ---------------------------------------------------------------------------
# Automatically notify the downstream webhook whenever a transaction's status
# changes, no matter which code path made the change. This is the single place
# that guarantees a status change is always reported.
# ---------------------------------------------------------------------------

_NOTIFY_STATUSES = {
    TransactionStatus.PROCESSING,
    TransactionStatus.SUCCESS,
    TransactionStatus.FAILED,
}


@receiver(pre_save, sender=TopupTransaction)
def _capture_previous_status(sender, instance, **kwargs):
    if instance.pk:
        instance._previous_status = (
            sender.objects.filter(pk=instance.pk)
            .values_list("status", flat=True)
            .first()
        )
    else:
        instance._previous_status = None


@receiver(post_save, sender=TopupTransaction)
def _notify_on_status_change(sender, instance, created, **kwargs):
    previous = getattr(instance, "_previous_status", None)
    if instance.status == previous:
        return
    # Skip the initial 'pending' (queued) state: the caller that created the
    # transaction already knows it, and re-queues also land on 'pending'.
    if instance.status not in _NOTIFY_STATUSES:
        return

    # Import here to avoid an import cycle at app-load time.
    from .webhooks import send_transaction_webhook

    transaction_id = instance.pk

    def _send():
        try:
            send_transaction_webhook(
                TopupTransaction.objects.select_related("sim_profile").get(
                    pk=transaction_id
                )
            )
        except TopupTransaction.DoesNotExist:
            pass
        except Exception:  # never let webhook errors break the save flow
            logger.exception(
                "Status-change webhook failed transaction=%s", transaction_id
            )

    # Send after the surrounding DB transaction commits (or immediately if
    # there is no open transaction).
    db_transaction.on_commit(_send)