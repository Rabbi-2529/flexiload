import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone

from .modem import (
    ModemError,
    carrier_has_transaction_id,
    carrier_response_is_failure,
    carrier_response_is_pending,
    carrier_response_is_success,
    extract_carrier_message,
    get_modem,
    package_reply_strategy,
    ussd_session_is_interactive,
    verify_sim_number,
)
from .models import TopupTransaction, TransactionStatus
from .ussd import build_ussd


logger = logging.getLogger("topups.transactions")


def execute_topup(user, data):
    """Queue a top-up.

    Recharges are NOT sent from here. Each request is stored as a PENDING
    transaction and the modem poller dispatches queued top-ups one at a time
    per SIM (a modem can only run one USSD session at a time). This makes it
    safe to submit many recharges at once — they line up and are sent in
    order, instead of colliding and timing out.
    """
    profile = data["sim_profile"]
    idempotency_key = data.get("idempotency_key")
    retry_failed = data.get("retry_failed", False)
    created = True
    transaction = None

    if idempotency_key:
        with db_transaction.atomic():
            existing = (
                TopupTransaction.objects.select_for_update()
                .filter(user=user, idempotency_key=idempotency_key)
                .first()
            )
            if existing:
                if (
                    not retry_failed
                    or existing.status != TransactionStatus.FAILED
                ):
                    return existing, False

                # Re-queue a failed transaction for another attempt.
                transaction = existing
                profile = transaction.sim_profile
                transaction.status = TransactionStatus.PENDING
                transaction.modem_response = ""
                transaction.error_message = ""
                transaction.completed_at = None
                transaction.dispatched_at = None
                transaction.retry_count += 1
                transaction.last_retried_at = timezone.now()
                transaction.save(
                    update_fields=[
                        "status",
                        "modem_response",
                        "error_message",
                        "completed_at",
                        "dispatched_at",
                        "retry_count",
                        "last_retried_at",
                    ]
                )
                created = False

    if created:
        try:
            transaction = TopupTransaction.objects.create(
                user=user,
                sim_profile=profile,
                operator=profile.operator,
                product=data["product"],
                phone_number=data["phone_number"],
                amount=data["amount"],
                idempotency_key=idempotency_key,
                status=TransactionStatus.PENDING,
            )
        except IntegrityError:
            transaction = TopupTransaction.objects.get(
                user=user,
                idempotency_key=idempotency_key,
            )
            return transaction, False

    # Store a supplied PIN now so the poller can dispatch later without it.
    supplied_pin = data.get("pin")
    if supplied_pin:
        profile.set_pin(supplied_pin)
        profile.save(update_fields=["encrypted_pin", "updated_at"])

    logger.info(
        "Top-up queued transaction=%s user=%s operator=%s product=%s "
        "phone=%s amount=%s retry_count=%s",
        transaction.id,
        user.pk,
        profile.operator,
        transaction.product,
        _mask_phone(transaction.phone_number),
        transaction.amount,
        transaction.retry_count,
    )
    return transaction, created


def dispatch_topup(transaction):
    """Send one queued top-up over the modem and record its result.

    Called by the modem poller, which guarantees only one recharge per SIM is
    dispatched at a time. Everything the send needs (operator, product, number,
    amount, PIN) is read from the transaction and its SIM profile.
    """
    profile = transaction.sim_profile

    transaction.status = TransactionStatus.PROCESSING
    transaction.modem_response = ""
    transaction.error_message = ""
    transaction.completed_at = None
    transaction.dispatched_at = timezone.now()
    transaction.save(
        update_fields=[
            "status",
            "modem_response",
            "error_message",
            "completed_at",
            "dispatched_at",
        ]
    )

    logger.info(
        "Top-up started transaction=%s operator=%s product=%s phone=%s "
        "amount=%s retry_count=%s",
        transaction.id,
        profile.operator,
        transaction.product,
        _mask_phone(transaction.phone_number),
        transaction.amount,
        transaction.retry_count,
    )

    try:
        sim_number, verification_response = verify_sim_number(
            profile.port or None,
            profile.operator,
        )

        profile.sim_number = sim_number
        profile.sim_verified_at = timezone.now()
        profile.sim_verification_response = verification_response
        profile.save(
            update_fields=[
                "sim_number",
                "sim_verified_at",
                "sim_verification_response",
                "updated_at",
            ]
        )

        pin = profile.get_pin()

        code, dcs = build_ussd(
            profile.operator,
            transaction.product,
            transaction.phone_number,
            transaction.amount,
            pin,
        )

        modem = get_modem()

        if profile.operator == "banglalink":
            # Amounts ending in 0 or 5 are plain cash recharges -> Default (0).
            # Other amounts on a prepaid recharge (any product except postpaid,
            # e.g. flexiload) are treated as package purchases: walk the menu
            # pages for a package priced exactly the amount, buy it if found,
            # otherwise fall back to Default.
            if (
                transaction.amount % 10 not in (0, 5)
                and transaction.product != "postpaid"
            ):
                result = modem.send_ussd_session(
                    code,
                    dcs=dcs,
                    port=profile.port or None,
                    reply_strategy=package_reply_strategy(transaction.amount),
                )
            else:
                result = modem.send_ussd_session(
                    code,
                    replies=["0"],
                    dcs=dcs,
                    port=profile.port or None,
                )
        else:
            result = modem.send_ussd(
                code,
                dcs=dcs,
                port=profile.port or None,
            )

        transaction.modem_response = result.response

        response_pending = carrier_response_is_pending(result.response)
        response_failed = carrier_response_is_failure(result.response)
        response_success = carrier_response_is_success(result.response)

        if response_pending:
            transaction.status = TransactionStatus.PROCESSING
            transaction.error_message = (
                "The carrier accepted the request and is processing it. "
                "Waiting for the confirmation SMS."
            )
        else:
            result_successful = response_success or (
                result.successful and not response_failed
            )

            if profile.operator == "banglalink":
                result_successful = (
                    carrier_has_transaction_id(result.response)
                    and response_success
                    and not response_failed
                )

            transaction.status = (
                TransactionStatus.SUCCESS
                if result_successful
                else TransactionStatus.FAILED
            )

        if transaction.status == TransactionStatus.FAILED:
            if response_failed:
                transaction.error_message = (
                    extract_carrier_message(result.response)
                    or "The carrier rejected the request."
                )
            elif ussd_session_is_interactive(result.response):
                transaction.error_message = (
                    "The carrier opened an interactive USSD menu; "
                    "the recharge was not completed. Review the "
                    "complete modem response."
                )
            elif profile.operator == "banglalink":
                transaction.error_message = (
                    extract_carrier_message(result.response)
                    or "Banglalink did not return a recharge transaction ID."
                )
            else:
                transaction.error_message = (
                    "The modem or carrier rejected the request."
                )

    except (ModemError, ValueError, ImproperlyConfigured) as exc:
        transaction.status = TransactionStatus.FAILED
        transaction.error_message = str(exc)
        logger.exception("Top-up failed transaction=%s", transaction.id)

    transaction.completed_at = (
        None
        if transaction.status == TransactionStatus.PROCESSING
        else timezone.now()
    )

    transaction.save(
        update_fields=[
            "status",
            "modem_response",
            "error_message",
            "completed_at",
        ]
    )

    logger.info(
        "Top-up completed transaction=%s status=%s retry_count=%s",
        transaction.id,
        transaction.status,
        transaction.retry_count,
    )

    # The webhook is sent automatically by the status-change signal on the
    # model whenever the status moves to processing/success/failed.
    return transaction


def _mask_phone(number):
    return f"{number[:3]}****{number[-4:]}"
